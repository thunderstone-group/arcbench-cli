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
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any

from . import __version__
from .client import (
    DEFAULT_REQUEST_LIMIT,
    ApiError,
    OfficialClient,
    RunQueueTimeoutError,
    SubmitConfig,
    aggregate_usage,
    enforce_budget,
    load_env,
    log_summary,
    progress_line,
    summarize_request,
    summarize_run,
    summarize_submission,
    usage_total,
    validate_package,
    write_record,
)
from .session import capture_chrome_cookie, default_env_path
from .preflight import LaunchPreflight, PreflightError, launch_lock

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


def _client(args: argparse.Namespace) -> tuple[dict[str, str], OfficialClient]:
    env, config = _config(args)
    if not config.session_cookie:
        raise CliError("ARC_BENCH_SESSION_COOKIE is not configured; run `arcbench session`")
    return env, OfficialClient(config)


def _meter_client(args: argparse.Namespace) -> tuple[dict[str, str], OfficialClient]:
    """Open a metering session, preferring the gateway key over a cookie.

    The metering site accepts the same access key the CLI hands to the model
    gateway, so no browser cookie is needed. ``ARC_BENCH_METER_COOKIE`` still
    wins when it is set, for an account whose key is not at hand.
    """
    env, config = _config(args)
    return env, _meter_from_config(config)


def _meter_from_config(config: SubmitConfig, website: OfficialClient | None = None) -> OfficialClient:
    meter = config.for_meter()
    client = OfficialClient(meter)
    if meter.session_cookie:
        return client
    key = meter.api_key
    if not key:
        if not config.session_cookie:
            raise CliError(
                "no metering credential: set ARC_BENCH_API_KEY, or a website session "
                "(`arcbench session`) so the account key can be read, or ARC_BENCH_METER_COOKIE"
            )
        key = (website or OfficialClient(config)).access_key()
    if not key:
        raise CliError("the account has no gateway access key to log in to the meter with")
    client.meter_login(key)
    return client


def _locked_launch(command):
    @wraps(command)
    def locked(args):
        if getattr(args, "dry_run", False):
            return command(args)
        with launch_lock():
            return command(args)
    return locked


def _launch_preflight(args, client, api_key=None) -> LaunchPreflight:
    config = client.config
    if api_key is not None:
        # Check the explicitly selected upload key, not an unrelated meter cookie.
        config = replace(config, api_key=api_key, meter_cookie="")
    return LaunchPreflight(
        client, lambda: _meter_from_config(config, client),
        getattr(args, "min_balance", "0"), getattr(args, "allow_concurrent", False),
    )


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


def _terminal_exit_code(runs: list[dict[str, Any]]) -> int:
    """Reduce one or many observed runs to one exit code.

    A run that reached a non-passing terminal state is definite, so it outranks
    a local deadline, which says nothing about the run at all.
    """
    finished = [run for run in runs if not run.get("poll_timed_out")]
    if any(str(run.get("status", "")).upper() != "PASSED" for run in finished):
        return 1
    return 2 if len(finished) < len(runs) else 0


def _resolve_tasks(
    client: OfficialClient,
    submission_id: str,
    tasks: list[str] | None,
    all_tasks: bool,
) -> list[str]:
    """Settle on the task list, from repeated --task or from the competition."""
    resolved = (
        client.tasks_for_submission(submission_id)
        if all_tasks
        else [str(task) for task in (tasks or [])]
    )
    if not resolved:
        raise CliError("choose what to run: --task TASK (repeatable) or --all-tasks")
    return list(dict.fromkeys(resolved))


# --- credentials ---------------------------------------------------------


def cmd_session(args: argparse.Namespace) -> int:
    env_path = _env_file(args) or default_env_path()
    path, length = capture_chrome_cookie(args.profile, env_path)
    log(f"wrote ARC_BENCH_SESSION_COOKIE to {path} ({length} chars, mode 600)")
    log("the value is never printed; re-run after the session expires")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    _, client = _meter_client(args) if args.meter else _client(args)
    result = client.check_login()
    emit(
        args,
        result,
        [
            f"logged_in={result.get('logged_in')} "
            f"user={result.get('username') or result.get('account')}"
        ],
    )
    return 0 if result.get("logged_in") else 2


def cmd_balance(args: argparse.Namespace) -> int:
    _, client = _meter_client(args)
    result = client.balance()
    emit(
        args,
        result,
        [
            f"account={result['account']} balance={result['available_balance']} "
            f"{result['currency'] or ''} as_of={result['as_of']} "
            f"pending={result['pending_billing_events']}"
        ],
    )
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    """Print the gateway's price table, as the metering site publishes it."""
    _, client = _meter_client(args)
    models = client.models()
    lines = []
    for model in models:
        pricing = model.get("pricing") or {}
        lines.append(
            f"{str(model.get('id', '-')):32s} {str(model.get('provider', '-')):12s} "
            f"available={str(model.get('available')):5s} "
            f"in/cache/out={pricing.get('input')}/{pricing.get('cache_hit')}/{pricing.get('output')} "
            f"{pricing.get('unit') or ''}"
        )
    emit(args, models, lines)
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    """Print metered usage per bucket and model, with a total.

    The meter ignores its own `granularity`, `since` and `model` parameters and
    always answers with the full hourly history, so the selection is made here.
    """
    _, client = _meter_client(args)
    rows = aggregate_usage(client.usage(args.granularity), args.granularity, args.since, args.model)
    lines = [
        f"{str((row.get('dimensions') or {}).get('bucket', '-')):26s} "
        f"{str((row.get('dimensions') or {}).get('model', '-')):28s} "
        f"amount={(row.get('measures') or {}).get('amount')} "
        f"prompt={(row.get('measures') or {}).get('prompt_tokens')} "
        f"completion={(row.get('measures') or {}).get('completion_tokens')} "
        f"cached={(row.get('measures') or {}).get('cached_tokens')} "
        f"events={(row.get('measures') or {}).get('usage_event_count')}"
        for row in rows
    ]
    total = usage_total(rows)
    lines.append(
        f"{'TOTAL':26s} {total['buckets']} bucket{'' if total['buckets'] == 1 else 's':17s} "
        f"amount={total['amount']} "
        f"prompt={total['prompt_tokens']} completion={total['completion_tokens']} "
        f"cached={total['cached_tokens']} events={total['usage_event_count']}"
    )
    emit(args, rows, lines)
    return 0


def cmd_requests(args: argparse.Namespace) -> int:
    """Print the newest billed gateway requests."""
    _, client = _meter_client(args)
    entries = client.request_log(args.limit)
    summaries = [summarize_request(entry) for entry in entries]
    lines = [
        f"{str(item['occurred_at']):22s} {str(item['model']):28s} "
        f"tokens={item['total_tokens']} (in {item['input_tokens']} / out {item['output_tokens']} "
        f"/ cached {item['cached_tokens']}) amount={item['amount']} {item['request_id']}"
        for item in summaries
    ]
    emit(args, entries, lines)
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


@_locked_launch
def cmd_run(args: argparse.Namespace) -> int:
    """Create and start one run per task. Two API calls each, never retried.

    The platform runs several tasks, and several submissions, at the same time,
    so the runs are started one after another and left to overlap. A task that
    fails to start does not stop the ones after it.
    """
    _, client = _client(args)
    tasks = _resolve_tasks(client, args.submission, args.task, args.all_tasks)
    timeout = _queue_timeout(args, client.config)
    preflight = _launch_preflight(args, client)
    checked = preflight.check(task_count=len(tasks))
    records: list[dict[str, Any]] = []
    lines: list[str] = []
    started_all = True
    uncertain_write = False
    for task in tasks:
        record: dict[str, Any] = {"task": task, "preflight": checked}
        if uncertain_write:
            record["start_error"] = {"reason": "previous_outcome_uncertain", "error": "inspect the previous write before launching more tasks"}
            records.append(record)
            lines.append(f"- {task} skipped after an uncertain write")
            continue
        run_id = ""
        try:
            if records:
                record["preflight"] = preflight.check()
            run = client.create_run(args.submission, task)
            run_id = str(run.get("id") or run.get("run_id"))
            record["run_id"] = run_id
            record["url"] = f"{client.config.base_url}/runs/{run_id}"
            started = client.start_run_with_queue_wait(
                run_id, timeout, on_wait=_on_queue_wait,
                before_start=lambda: preflight.check(target_run=run_id),
            )
            record["run"] = summarize_run(started)
            lines.append(f"{run_id} {task} {started.get('status')}")
        except (ApiError, RunQueueTimeoutError) as error:
            started_all = False
            uncertain_write = isinstance(error, ApiError) and error.uncertain
            record["start_error"] = (
                error.details if isinstance(error, ApiError) else {"error": str(error)}
            )
            if run_id:
                record["next"] = f"inspect the run, then use: arcbench start {run_id}"
                lines.append(f"{run_id} {task} created but not started: {error}")
            else:
                lines.append(f"- {task} not created: {error}")
        records.append(record)
    emit(args, records, lines)
    return 0 if started_all else 1


@_locked_launch
def cmd_start(args: argparse.Namespace) -> int:
    if not args.run:
        raise CliError("start needs a run id, as a positional argument or --run-id")
    _, client = _client(args)
    timeout = _queue_timeout(args, client.config)
    current = client.get_run(args.run)
    if current.get("status") != "PENDING":
        raise PreflightError("run_not_pending", "start requires a confirmed PENDING run", run_id=args.run)
    preflight = _launch_preflight(args, client)
    checked = preflight.check(target_run=args.run)
    run = client.start_run_with_queue_wait(
        args.run, timeout, on_wait=_on_queue_wait,
        before_start=lambda: preflight.check(target_run=args.run),
    )
    summary = summarize_run(run)
    summary["preflight"] = checked
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
    """Wait for one run, or for several at once, polling them round-robin."""
    _, client = _client(args)
    seen: dict[str, str] = {}

    def tick(run_id: str, run: dict[str, Any]) -> None:
        summary = summarize_run(run)
        signature = json.dumps(summary, sort_keys=True, ensure_ascii=False)
        if seen.get(run_id) != signature:
            seen[run_id] = signature
            emit(args, summary, [f"  {run_id} {progress_line(run)}"])

    def retry(run_id: str, attempt: int, error: ApiError) -> None:
        emit(args, {"run_id": run_id, "waiting_retry": attempt, **error.details},
             [f"  {run_id} transport failure {attempt}; re-reading: {error}"])

    runs = client.poll_runs(args.run, args.interval, args.timeout, on_tick=tick, on_retry=retry)
    for run in runs:
        if run.get("poll_timed_out"):
            run_id = run.get("id")
            emit(
                args,
                {"run_id": run_id, "waiting_timed_out": True, "run_cancelled": False},
                [f"waiting deadline expired for {run_id}; the remote run was not cancelled"],
            )
    return _terminal_exit_code(runs)


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
    runs = []
    for run_id in args.run:
        run = client.get_run(run_id)
        runs.append(run)
        record = client.safe(run) if args.full else summarize_run(run)
        emit(args, record, [f"result: {json.dumps(record, ensure_ascii=False)}"])
    return _terminal_exit_code(runs)


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


@_locked_launch
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
    if args.all_tasks:
        if not task_ids:
            raise CliError(f"{args.competition} publishes no tasks to run")
        tasks = task_ids
    else:
        tasks = list(dict.fromkeys(args.task or []))
        if not tasks:
            raise CliError("choose what to run: --task TASK (repeatable) or --all-tasks")
        unknown = [task for task in tasks if task_ids and task not in task_ids]
        if unknown:
            raise CliError(f"tasks {unknown} are not in {args.competition}: {task_ids}")

    log(f"package sha256={package['sha256'][:12]}... bytes={package['bytes']}")
    log(f"competition={args.competition} tasks={','.join(tasks)} model={model} name={name}")
    if args.dry_run:
        log("dry-run: nothing submitted")
        return 0

    timeout = _queue_timeout(args, config)
    api_key = _resolve_key(args) if not args.submission_id else None
    preflight = _launch_preflight(args, client, api_key)
    checked = preflight.check(task_count=len(tasks))
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
            api_key,
        )
        submission_id = uploaded["submission"].get("id")
        verified = uploaded["archive_verified"]
        if not submission_id:
            raise CliError("submission response had no id")
        log(f"submission created: {submission_id} archive_verified={verified}")
        if not verified:
            error = ApiError("saved archive could not be verified; inspect the submission before starting")
            error.details.update(reason="archive_unverified", submission_id=submission_id)
            raise error

    # Every task is started before any is waited on: the platform runs them
    # concurrently, and serialising them here would only make the batch slower.
    started: list[tuple[str, str, dict[str, Any]]] = []
    for task in tasks:
        preflight.check()
        run = client.create_run(str(submission_id), task)
        run_id = str(run.get("id") or run.get("run_id"))
        log(f"run created: {run_id}  task={task}  status={run.get('status', 'PENDING')}")
        run = client.start_run_with_queue_wait(
            run_id, timeout, on_wait=_on_queue_wait,
            before_start=lambda: preflight.check(target_run=run_id),
        )
        log(f"run started: {run_id}  {config.base_url}/runs/{run_id}")
        started.append((task, run_id, run))

    seen: dict[str, str] = {}

    def tick(run_id: str, current: dict[str, Any]) -> None:
        line = progress_line(current)
        if seen.get(run_id) != line:
            seen[run_id] = line
            log(f"  {run_id} {line}")

    runs = [run for _, _, run in started]
    if not args.no_wait:
        runs = client.poll_runs(
            [run_id for _, run_id, _ in started],
            args.poll_interval,
            args.poll_timeout,
            on_tick=tick,
            on_retry=lambda run_id, attempt, error: log(
                f"  {run_id} transport failure {attempt}; re-reading"
            ),
        )

    for (task, run_id, _), run in zip(started, runs):
        metrics = summarize_run(run)
        record = {
            "record_type": "competition-official",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "submission_name": name,
            "submission_id": submission_id,
            "archive_verified": verified,
            "competition": args.competition,
            "task": task,
            "model": model,
            "preflight": checked,
            "package": package,
            "metrics": metrics,
            "run_url": f"{config.base_url}/runs/{run_id}",
        }
        path = _record_path(record_dir, args.competition, task)
        write_record(path, record, tuple(client.known_secrets))
        log(f"result: {json.dumps(metrics, ensure_ascii=False)}")
        log(f"record: {path}")
    return 0 if args.no_wait else _terminal_exit_code(runs)


def resolve_run_id(args: argparse.Namespace) -> argparse.Namespace:
    """Accept run ids positionally or as --run-id, and settle on one field."""
    run_id = getattr(args, "run_id", None)
    current = getattr(args, "run", None)
    if run_id and not current:
        args.run = [run_id] if isinstance(current, list) else run_id
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

    add("models", "list the gateway's models and their published prices", cmd_models)

    command = add("usage", "read metered usage per bucket and model", cmd_usage)
    command.add_argument("--granularity", choices=("hour", "day"), default="hour")
    command.add_argument("--since", help="keep buckets at or after this ISO timestamp")
    command.add_argument("--model", help="keep only this model's rows")

    command = add("requests", "read the newest billed gateway requests", cmd_requests)
    command.add_argument("--limit", type=int, default=DEFAULT_REQUEST_LIMIT)

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

    def launch_options(command):
        command.add_argument("--min-balance", default="0", help="minimum available balance in the meter currency; balance must also be positive")
        command.add_argument("--allow-concurrent", action="store_true", help="explicitly accept overlapping runs and contaminated per-run cost; never bypass balance, pending billing or lock checks")

    command = add("run", "create and start one run per task, from a saved submission", cmd_run)
    launch_options(command)
    command.add_argument("submission")
    command.add_argument(
        "--task", action="append", help="task to run; repeat for several tasks"
    )
    command.add_argument(
        "--all-tasks",
        action="store_true",
        help="run every task of the submission's competition",
    )
    command.add_argument("--queue-timeout", type=float, default=None)

    command = add("start", "start an existing PENDING run, waiting for queue capacity", cmd_start)
    launch_options(command)
    command.add_argument("run", nargs="?")
    command.add_argument("--run-id", help="alternative spelling of the positional run id")
    command.add_argument("--queue-timeout", type=float, default=None)

    command = add("cancel", "cancel one run, then read the resulting state", cmd_cancel)
    command.add_argument("run")

    command = add("status", "list runs, or read one or more runs", cmd_status)
    command.add_argument("run", nargs="*")
    command.add_argument("--run-id", help="alternative spelling of the positional run id")
    command.add_argument("--limit", type=int, default=10)
    command.add_argument("--full", action="store_true", help="print the redacted full response")

    command = add(
        "wait", "poll runs until they finish or the deadline expires", cmd_wait
    )
    command.add_argument("run", nargs="+")
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
    launch_options(command)
    command.add_argument("--package", required=True)
    command.add_argument("--competition", required=True)
    command.add_argument(
        "--task", action="append", help="task to run; repeat for several tasks"
    )
    command.add_argument(
        "--all-tasks", action="store_true", help="run every task of the competition"
    )
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
