"""Command-line interface for ARC-Bench.

One command surface over one credential model. Every command but ``session``
is plain HTTP against the platform API; ``session`` reads the browser cookie
store once per login.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .client import (
    ApiError,
    OfficialClient,
    RunQueueTimeoutError,
    SubmitConfig,
    enforce_budget,
    load_env,
    log_summary,
    progress_line,
    summarize_run,
    summarize_submission,
    validate_package,
    write_record,
)
from .session import capture_chrome_cookie, default_env_path

PACKAGE_EXCLUDED = {"node_modules", ".git", "dist", "__pycache__", ".venv", ".pytest_cache"}
ENTRYPOINTS = ("main.py", "index.js", "index.ts")


class CliError(RuntimeError):
    """An expected user-facing error."""


def log(message: str) -> None:
    print(f"[arcbench] {message}", flush=True)


def emit(args: argparse.Namespace, record: Any, lines: list[str] | None = None) -> None:
    """Print one record as compact JSON, or as human lines.

    A single flag rather than two output surfaces: agents pass ``--json``,
    people read the default.
    """
    if getattr(args, "json", False):
        print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), flush=True)
        return
    if lines is None:
        lines = [json.dumps(record, ensure_ascii=False)]
    for line in lines:
        print(line, flush=True)


def _env_file(args: argparse.Namespace) -> Path | None:
    configured = getattr(args, "env_file", None) or os.environ.get("ARCBENCH_ENV_FILE")
    return Path(configured).expanduser() if configured else None


def _config(args: argparse.Namespace) -> tuple[dict[str, str], SubmitConfig]:
    env = load_env(_env_file(args))
    config = SubmitConfig.from_env(env)
    if getattr(args, "base_url", None):
        config.base_url = args.base_url.rstrip("/")
    return env, config


def _client(args: argparse.Namespace, meter: bool = False) -> tuple[dict[str, str], OfficialClient]:
    env, config = _config(args)
    if meter:
        config = config.for_meter()
        if not config.session_cookie:
            raise CliError(
                "ARC_BENCH_METER_COOKIE is not configured; the metering site has its own login"
            )
    elif not config.session_cookie:
        raise CliError("ARC_BENCH_SESSION_COOKIE is not configured; run `arcbench session`")
    return env, OfficialClient(config)


def _default_record_dir(env: dict[str, str]) -> Path:
    configured = env.get("ARC_BENCH_RECORD_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / ".arcbench" / "runs" / "submissions").resolve()


def _queue_timeout(args: argparse.Namespace, config: SubmitConfig) -> float:
    timeout = getattr(args, "queue_timeout", None)
    if timeout is None:
        timeout = config.queue_timeout_seconds
    timeout = float(timeout)
    if timeout < 0:
        raise CliError("--queue-timeout must be non-negative")
    return timeout


def _new_file(path: Path) -> Path:
    """Refuse to overwrite an existing artifact."""
    resolved = Path(path).expanduser().resolve()
    if resolved.exists():
        raise CliError(f"output already exists; choose a new path: {resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _record_path(record_dir: Path, competition: str, task: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return record_dir / f"{stamp}-{competition}-{task}.json"


def _on_queue_wait(attempt: int, retry_after: float, delay: float, status: int, payload: Any) -> None:
    log(
        f"queue full (HTTP {status}); waiting {delay:.1f}s "
        f"(attempt {attempt}, Retry-After={retry_after:.1f}s)"
    )


def _terminal_exit_code(run: dict[str, Any]) -> int:
    if run.get("poll_timed_out"):
        return 2
    return 0 if str(run.get("status", "")).upper() == "PASSED" else 1


# --- credentials ---------------------------------------------------------


def cmd_session(args: argparse.Namespace) -> int:
    env_path = _env_file(args) or default_env_path()
    path, length = capture_chrome_cookie(args.profile, env_path)
    log(f"wrote ARC_BENCH_SESSION_COOKIE to {path} ({length} chars, mode 600)")
    log("the value is never printed; re-run after the session expires")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    _, client = _client(args, meter=args.meter)
    result = client.check_login()
    emit(args, result, [f"logged_in={result.get('logged_in')} user={result.get('username')}"])
    return 0 if result.get("logged_in") else 2


def cmd_balance(args: argparse.Namespace) -> int:
    _, client = _client(args, meter=True)
    result = client.balance()
    emit(
        args,
        result,
        [
            f"balance={result['available_balance']} {result['currency'] or ''} "
            f"as_of={result['as_of']} pending={result['pending_billing_events']}"
        ],
    )
    return 0


# --- discovery -----------------------------------------------------------


def _competition_line(competition: dict[str, Any]) -> str:
    return (
        f"{str(competition.get('id', '-')):26s} tasks={competition.get('task_count')} "
        f"tests={competition.get('total_tests')} status={competition.get('status')}"
    )


def cmd_tasks(args: argparse.Namespace) -> int:
    _, client = _client(args)
    competition_id = getattr(args, "competition", None)
    if competition_id:
        detail = client.get_competition(competition_id)
        tasks = detail.get("tasks") or []
        record = {
            **{
                key: detail[key]
                for key in ("id", "title", "task_count", "total_tests", "template_required")
                if key in detail
            },
            "tasks": [
                {
                    key: task[key]
                    for key in ("id", "display_id", "title", "category", "total_tests", "module_count")
                    if key in task
                }
                for task in tasks
            ],
        }
        emit(
            args,
            record,
            [_competition_line(detail)]
            + [
                f"    {str(task.get('id', '-')):38s} tests={task.get('total_tests')} "
                f"modules={task.get('module_count')}"
                for task in tasks
            ],
        )
        return 0
    competitions = client.list_competitions()
    record = [
        {
            key: item[key]
            for key in ("id", "title", "status", "task_count", "total_tests", "template_required")
            if key in item
        }
        for item in competitions
    ]
    lines = [_competition_line(item) for item in competitions]
    if getattr(args, "verbose", False):
        for item in competitions:
            for task in client.get_competition(str(item.get("id"))).get("tasks") or []:
                lines.append(
                    f"    {str(task.get('id', '-')):38s} tests={task.get('total_tests')} "
                    f"modules={task.get('module_count')}"
                )
    emit(args, record, lines)
    return 0


def _write_pack(client: OfficialClient, out: Path, task_id: str) -> dict[str, Any]:
    payload = client.get_test_pack(task_id)
    files = payload.get("files") or []
    if not files:
        raise CliError(f"no test files returned for {task_id}")
    out.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, Any]] = []
    for entry in files:
        relative = entry.get("path") or ""
        content = entry.get("content") or ""
        if not relative or ".." in Path(relative).parts or relative.startswith(("/", "\\")):
            continue
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append({"path": relative, "bytes": len(content.encode("utf-8"))})
    manifest = {
        "label": "competition-official",
        "source": f"{client.config.base_url}/api/requirements/{task_id}/tests?catalog=competition",
        "task_id": task_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(written),
        "files": written,
    }
    (out / "fetch-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


def cmd_fetch(args: argparse.Namespace) -> int:
    _, client = _client(args)
    root = Path(args.out or (Path.cwd() / "testsets" / "official")).expanduser().resolve()
    detail = client.get_competition(args.competition)
    tasks = detail.get("tasks") or []
    if not tasks:
        log(f"{args.competition} publishes no tasks yet")
        return 1
    index: list[dict[str, Any]] = []
    for task in tasks:
        task_id = task.get("id")
        if not task_id:
            continue
        entry = _write_pack(client, root / str(task_id), str(task_id))
        entry.update({"title": task.get("title"), "total_tests": task.get("total_tests")})
        index.append(entry)
        log(f"{task_id}: {entry['file_count']} files, {task.get('total_tests')} tests")
    record = {
        "competition_id": args.competition,
        "title": detail.get("title"),
        "total_tests": detail.get("total_tests"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "tasks": index,
    }
    (root / "index.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    log(f"wrote {len(index)} packs to {root}")
    return 0


def cmd_submissions(args: argparse.Namespace) -> int:
    _, client = _client(args)
    items = [summarize_submission(item) for item in client.list_submissions(args.competition)]
    emit(
        args,
        items,
        [
            f"{item.get('id')}  {str(item.get('competition_id')):20s} "
            f"{item.get('display_name')}  {item.get('created_at')}"
            for item in items
        ],
    )
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    _, client = _client(args)
    runs = client.list_runs(args.limit, args.task, args.submission)
    items = [summarize_run(run) for run in runs]
    emit(
        args,
        items,
        [
            f"{item.get('run_id')}  {str(item.get('status')):9s} "
            f"{item.get('requirement_id')}  passed={item.get('passed')} "
            f"failed={item.get('failed')}"
            for item in items
        ],
    )
    return 0


def cmd_leaderboard(args: argparse.Namespace) -> int:
    _, client = _client(args)
    rows = client.leaderboard(args.competition, args.task, args.team, args.limit)
    emit(
        args,
        rows,
        [
            f"{row['rank']:3d}  {str(row.get('username')):24s} "
            f"pass_rate={row.get('avg_pass_rate')} tokens={row.get('avg_token_count')}"
            for row in rows
        ],
    )
    return 0


# --- packaging -----------------------------------------------------------


def cmd_package(args: argparse.Namespace) -> int:
    """Build a submission ZIP from a directory of agent sources."""
    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        raise CliError(f"source directory not found: {source}")
    present = [name for name in ENTRYPOINTS if (source / name).is_file()]
    if not present:
        raise CliError(
            f"{source} has no root entrypoint; expected one of {', '.join(ENTRYPOINTS)}"
        )
    out = _new_file(Path(args.out))
    members = sorted(
        path
        for path in source.rglob("*")
        if path.is_file() and not PACKAGE_EXCLUDED.intersection(path.relative_to(source).parts)
    )
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in members:
            archive.write(path, path.relative_to(source).as_posix())
    record = validate_package(out)
    record["file_count"] = len(members)
    emit(args, record, [f"{out}", f"bytes={record['bytes']} sha256={record['sha256']}"])
    return 0


# --- submission and runs -------------------------------------------------


def _resolve_key(args: argparse.Namespace) -> str | None:
    name = getattr(args, "api_key_env", None)
    if not name:
        return None
    key = os.environ.get(name)
    if not key:
        raise CliError(f"missing model key environment variable: {name}")
    return key


def cmd_upload(args: argparse.Namespace) -> int:
    _, client = _client(args)
    package = Path(args.package).expanduser().resolve()
    validate_package(package)
    result = client.upload(
        package,
        args.competition,
        args.name or package.stem,
        args.model or client.config.model,
        args.runtime,
        _resolve_key(args),
    )
    submission_id = result["submission"].get("id")
    emit(
        args,
        result,
        [
            f"submission={submission_id} archive_verified={result['archive_verified']} "
            f"sha256={result['uploaded_sha256'][:12]}..."
        ],
    )
    return 0 if result["archive_verified"] else 1


def cmd_run(args: argparse.Namespace) -> int:
    """Create a run, then start it. Two API calls, neither ever retried."""
    _, client = _client(args)
    run = client.create_run(args.submission, args.task)
    run_id = str(run.get("id") or run.get("run_id"))
    record: dict[str, Any] = {
        "run_id": run_id,
        "url": f"{client.config.base_url}/runs/{run_id}",
    }
    try:
        started = client.start_run_with_queue_wait(
            run_id, _queue_timeout(args, client.config), on_wait=_on_queue_wait
        )
        record["run"] = summarize_run(started)
    except ApiError as error:
        record["start_error"] = error.details
        record["next"] = f"inspect the run, then use: arcbench start {run_id}"
        emit(args, record, [f"run={run_id} created but not started: {error}"])
        return 1
    emit(args, record, [f"run={run_id} {record['url']}"])
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    if not args.run:
        raise CliError("start needs a run id, as a positional argument or --run-id")
    _, client = _client(args)
    run = client.start_run_with_queue_wait(
        args.run, _queue_timeout(args, client.config), on_wait=_on_queue_wait
    )
    summary = summarize_run(run)
    emit(args, summary, [f"run started: {args.run}", f"result: {json.dumps(summary, ensure_ascii=False)}"])
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    """Send one cancellation and report the state that follows it.

    A 500 reply does not mean the run kept running; the state read after it is
    the authority.
    """
    _, client = _client(args)
    result = client.cancel_run(args.run)
    emit(
        args,
        result,
        [
            f"cancel_accepted={result['cancel_accepted']} "
            f"status={result['run'].get('status')}"
        ],
    )
    return 0 if result["cancel_accepted"] else 1


def cmd_wait(args: argparse.Namespace) -> int:
    _, client = _client(args)
    seen: list[str] = []

    def tick(run: dict[str, Any]) -> None:
        summary = summarize_run(run)
        signature = json.dumps(summary, sort_keys=True, ensure_ascii=False)
        if signature not in seen:
            seen.append(signature)
            emit(args, summary, [f"  {progress_line(run)}"])

    def retry(attempt: int, error: ApiError) -> None:
        emit(args, {"run_id": args.run, "waiting_retry": attempt, **error.details},
             [f"  transport failure {attempt}; re-reading: {error}"])

    run = client.poll_run(args.run, args.interval, args.timeout, on_tick=tick, on_retry=retry)
    if run.get("poll_timed_out"):
        emit(
            args,
            {"run_id": args.run, "waiting_timed_out": True, "run_cancelled": False},
            ["waiting deadline expired; the remote run was not cancelled"],
        )
        return 2
    return _terminal_exit_code(run)


def cmd_status(args: argparse.Namespace) -> int:
    _, client = _client(args)
    if not args.run:
        runs = client.list_runs(args.limit)
        items = [summarize_run(run) for run in runs]
        emit(
            args,
            items,
            [
                f"{item.get('run_id')}  {str(item.get('status')):9s} "
                f"{item.get('requirement_id')}  {item.get('display_name')}"
                for item in items
            ],
        )
        return 0
    run = client.get_run(args.run)
    record = client.safe(run) if args.full else summarize_run(run)
    emit(args, record, [f"result: {json.dumps(record, ensure_ascii=False)}"])
    return _terminal_exit_code(run)


def cmd_logs(args: argparse.Namespace) -> int:
    _, client = _client(args)
    if args.tail <= 0 or args.offset < 0:
        raise CliError("--tail must be positive and --offset must be non-negative")
    # Register the account key so it cannot surface in a log body.
    client.access_key()
    payload = client.get_run_logs(args.run, args.offset)
    summary = client.safe(log_summary(args.run, payload, args.tail))
    if args.out:
        out_dir = Path(args.out).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        logs_path = out_dir / f"{args.run}-logs.json"
        logs_path.write_text(
            json.dumps(client.safe(payload), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        summary["saved"] = str(logs_path)
        requirements = extract_marked_block(f"{summary['console']}\n{summary['stderr']}")
        if requirements:
            requirements_path = out_dir / "requirements.md"
            requirements_path.write_text(requirements, encoding="utf-8")
            summary["requirements"] = str(requirements_path)
    emit(args, summary, [summary["console"], summary["stderr"], f"next_offset={summary['next_offset']}"])
    return 0


def extract_marked_block(
    text: str,
    begin: str = "REQUIREMENTS-BEGIN",
    end: str = "REQUIREMENTS-END",
) -> str | None:
    """Extract a marked block while removing runner log-line prefixes."""
    start = text.find(begin)
    finish = text.find(end, start + len(begin)) if start >= 0 else -1
    if start < 0 or finish < 0:
        return None
    newline = text.find("\n", start)
    if newline < 0 or newline >= finish:
        return None
    lines = [
        re.sub(r"^\[[^\]]+\] \[runner\] [\w.-]+ \| ?", "", line)
        for line in text[newline + 1 : finish].splitlines()
    ]
    return "\n".join(lines).strip() + "\n"


def _save_bytes(args: argparse.Namespace, raw: bytes, output: Path) -> int:
    target = _new_file(output)
    with target.open("xb") as stream:
        stream.write(raw)
    record = {"path": str(target), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    emit(args, record, [f"{target} ({record['bytes']} bytes, sha256={record['sha256'][:12]}...)"])
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    _, client = _client(args)
    return _save_bytes(args, client.download_workspace(args.run), Path(args.output))


def cmd_archive(args: argparse.Namespace) -> int:
    _, client = _client(args)
    return _save_bytes(args, client.download_submission_archive(args.submission), Path(args.output))


def cmd_source(args: argparse.Namespace) -> int:
    """Read one workspace file without downloading the whole artifact."""
    _, client = _client(args)
    client.access_key()
    payload = client.get_run_source(args.run, args.file)
    content = client.safe(payload["content"]).encode("utf-8")
    target = _new_file(Path(args.output))
    handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "wb") as stream:
        stream.write(content)
    record = {
        "path": str(target),
        "file_path": payload.get("file_path"),
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    emit(args, record, [f"{target} ({record['bytes']} bytes)"])
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    """Upload, start one task and record the result, in one command."""
    env, config = _config(args)
    package = validate_package(Path(args.package).expanduser().resolve())
    name = args.name or Path(args.package).stem
    model = args.model or config.model
    record_dir = (
        Path(args.record_dir).expanduser().resolve()
        if args.record_dir
        else _default_record_dir(env)
    )
    if not config.session_cookie:
        raise CliError("ARC_BENCH_SESSION_COOKIE is not configured; run `arcbench session`")

    client = OfficialClient(config)
    login = client.check_login()
    log(f"login: {json.dumps(login, ensure_ascii=False)}")
    if not login.get("logged_in"):
        return 2

    task_ids = [str(task.get("id")) for task in client.list_tasks(args.competition)]
    if task_ids and args.task not in task_ids:
        raise CliError(f"task {args.task!r} is not in {args.competition}: {task_ids}")

    log(f"package sha256={package['sha256'][:12]}... bytes={package['bytes']}")
    log(f"competition={args.competition} task={args.task} model={model} name={name}")
    if args.dry_run:
        log("dry-run: nothing submitted")
        return 0

    enforce_budget(record_dir, config)

    if args.submission_id:
        submission_id = args.submission_id
        verified = None
        log(f"reusing submission {submission_id}")
    else:
        uploaded = client.upload(
            Path(args.package).expanduser().resolve(),
            args.competition,
            name,
            model,
            args.runtime,
            _resolve_key(args),
        )
        submission_id = uploaded["submission"].get("id")
        verified = uploaded["archive_verified"]
        if not submission_id:
            raise CliError("submission response had no id")
        log(f"submission created: {submission_id} archive_verified={verified}")

    run = client.create_run(str(submission_id), args.task)
    run_id = str(run.get("id") or run.get("run_id"))
    log(f"run created: {run_id}  status={run.get('status', 'PENDING')}")

    run = client.start_run_with_queue_wait(
        run_id, _queue_timeout(args, config), on_wait=_on_queue_wait
    )
    log(f"run started: {run_id}  {config.base_url}/runs/{run_id}")

    seen: list[str] = []

    def tick(current: dict[str, Any]) -> None:
        line = progress_line(current)
        if line not in seen:
            seen.append(line)
            log(f"  {line}")

    if not args.no_wait:
        run = client.poll_run(
            run_id,
            args.poll_interval,
            args.poll_timeout,
            on_tick=tick,
            on_retry=lambda attempt, error: log(f"  transport failure {attempt}; re-reading"),
        )

    metrics = summarize_run(run)
    record = {
        "record_type": "competition-official",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "submission_name": name,
        "submission_id": submission_id,
        "archive_verified": verified,
        "competition": args.competition,
        "task": args.task,
        "model": model,
        "package": package,
        "metrics": metrics,
        "run_url": f"{config.base_url}/runs/{run_id}",
    }
    path = _record_path(record_dir, args.competition, args.task)
    write_record(path, record, tuple(client.known_secrets))
    log(f"result: {json.dumps(metrics, ensure_ascii=False)}")
    log(f"record: {path}")
    return 0 if args.no_wait else _terminal_exit_code(run)


def resolve_run_id(args: argparse.Namespace) -> argparse.Namespace:
    """Accept a run id positionally or as --run-id, and settle on one field."""
    if getattr(args, "run_id", None) and not getattr(args, "run", None):
        args.run = args.run_id
    return args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arcbench",
        description=(
            "Discover ARC-Bench competitions, fetch official test packs, submit an "
            "agent, and inspect runs over the platform's own HTTP API."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url", default=None, help="platform base URL; overrides ARC_BENCH_WEB_BASE_URL"
    )
    parser.add_argument(
        "--env-file", default=None, help="env file to read; overrides ARCBENCH_ENV_FILE"
    )
    parser.add_argument("--json", action="store_true", help="emit one compact JSON record")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    # The same global flags are accepted after the subcommand, where people
    # reach for them. SUPPRESS keeps an unused sub-level flag from overwriting
    # the value given before the subcommand.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--base-url", default=argparse.SUPPRESS)
    shared.add_argument("--env-file", default=argparse.SUPPRESS)
    shared.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    def add(name: str, help_text: str, handler) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text, parents=[shared])
        command.set_defaults(func=handler)
        return command

    command = add("session", "capture the local Chrome session into an env file", cmd_session)
    command.add_argument("--profile", default="Default", help="Chrome profile name")

    command = add("whoami", "check the configured session", cmd_whoami)
    command.add_argument("--meter", action="store_true", help="check the metering session instead")

    add("balance", "read the metering balance and billing freshness", cmd_balance)

    command = add("tasks", "list competitions, or one competition's tasks", cmd_tasks)
    command.add_argument("competition", nargs="?", help="competition id; omit to list competitions")
    command.add_argument("-v", "--verbose", action="store_true", help="also list every task")

    # `competitions` is the name the platform docs use for the bare list.
    command = add("competitions", "list competitions", cmd_tasks)
    command.set_defaults(competition=None, verbose=False)

    command = add("fetch", "download a competition's official test packs", cmd_fetch)
    command.add_argument("--competition", required=True)
    command.add_argument("--out", help="output root; defaults to ./testsets/official")

    command = add("submissions", "list saved submissions", cmd_submissions)
    command.add_argument("--competition")

    command = add("runs", "list runs, newest first", cmd_runs)
    command.add_argument("--task", help="filter by requirement id")
    command.add_argument("--submission", help="filter by submission id")
    command.add_argument("--limit", type=int, default=10)

    command = add("leaderboard", "read a competition or task leaderboard", cmd_leaderboard)
    command.add_argument("--competition", required=True)
    command.add_argument("--task", help="task slug or full requirement id")
    command.add_argument("--team", help="keep only this team's rows, after ranking")
    command.add_argument("--limit", type=int, default=10)

    command = add("package", "build a submission ZIP from a source directory", cmd_package)
    command.add_argument("--from", dest="source", required=True, help="directory to package")
    command.add_argument("--out", required=True, help="output ZIP path")

    command = add("upload", "save a submission and verify its stored archive", cmd_upload)
    command.add_argument("package")
    command.add_argument("--competition", required=True)
    command.add_argument("--name")
    command.add_argument("--model")
    command.add_argument("--runtime", choices=("python", "node"), default="python")
    command.add_argument("--api-key-env", help="read the model key from this environment variable")

    command = add("run", "create a run from a saved submission, then start it", cmd_run)
    command.add_argument("submission")
    command.add_argument("--task", required=True)
    command.add_argument("--queue-timeout", type=float, default=None)

    command = add("start", "start an existing PENDING run, waiting for queue capacity", cmd_start)
    command.add_argument("run", nargs="?")
    command.add_argument("--run-id", help="alternative spelling of the positional run id")
    command.add_argument("--queue-timeout", type=float, default=None)

    command = add("cancel", "cancel one run, then read the resulting state", cmd_cancel)
    command.add_argument("run")

    command = add("status", "list runs, or read one run", cmd_status)
    command.add_argument("run", nargs="?")
    command.add_argument("--run-id", help="alternative spelling of the positional run id")
    command.add_argument("--limit", type=int, default=10)
    command.add_argument("--full", action="store_true", help="print the redacted full response")

    command = add("wait", "poll one run until it finishes or the deadline expires", cmd_wait)
    command.add_argument("run")
    command.add_argument("--interval", type=float, default=10.0)
    command.add_argument("--timeout", type=float, default=1200.0)

    command = add("logs", "read a bounded log tail and its cursor", cmd_logs)
    command.add_argument("run")
    command.add_argument("--offset", type=int, default=0)
    command.add_argument("--tail", type=int, default=40)
    command.add_argument("--out", help="also save the full log JSON to this directory")

    command = add("download", "download one run's generated workspace bundle", cmd_download)
    command.add_argument("run")
    command.add_argument("--output", required=True)

    command = add("archive", "download a saved submission's archive", cmd_archive)
    command.add_argument("submission")
    command.add_argument("--output", required=True)

    command = add("source", "read one workspace file from a run", cmd_source)
    command.add_argument("run")
    command.add_argument("file")
    command.add_argument("--output", required=True)

    command = add("submit", "upload, start one task, and record the result", cmd_submit)
    command.add_argument("--package", required=True)
    command.add_argument("--competition", required=True)
    command.add_argument("--task", required=True)
    command.add_argument("--name")
    command.add_argument("--model")
    command.add_argument("--runtime", choices=("python", "node"), default="python")
    command.add_argument("--submission-id", help="reuse an existing submission")
    command.add_argument("--api-key-env", help="read the model key from this environment variable")
    command.add_argument("--record-dir", help="where JSON result records are written")
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--no-wait", action="store_true")
    command.add_argument("--poll-interval", type=float, default=20.0)
    command.add_argument("--poll-timeout", type=float, default=3600.0)
    command.add_argument("--queue-timeout", type=float, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = resolve_run_id(build_parser().parse_args(argv))
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    except BrokenPipeError:
        return 0
    except ApiError as error:
        if getattr(args, "json", False):
            print(json.dumps(error.details, ensure_ascii=False), file=sys.stderr, flush=True)
        else:
            log(str(error))
        return 1
    except RunQueueTimeoutError as error:
        log(str(error))
        return 2
    except (CliError, RuntimeError, ValueError, OSError) as error:
        log(str(error))
        return 1
    except urllib.error.URLError as error:
        log(f"network error: {error.reason}")
        return 1
