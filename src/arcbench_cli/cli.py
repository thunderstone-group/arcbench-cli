"""Command-line interface for the ARC-Bench submission workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .client import (
    DEFAULT_BASE_URL,
    OfficialClient,
    SubmitConfig,
    enforce_budget,
    load_env,
    progress_line,
    summarize_run,
    validate_package,
    write_record,
)
from .session import capture_chrome_cookie, default_env_path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT_FILES = ("main.py", "requirements.txt", "package.json", "agent.mjs")
TEMPLATE_EXCLUDED = {"node_modules", ".git", "dist", "__pycache__"}


class CliError(RuntimeError):
    """An expected user-facing error."""


def log(message: str) -> None:
    print(f"[arcbench] {message}", flush=True)


def _base_url(args: argparse.Namespace, env: dict[str, str] | None = None) -> str:
    if getattr(args, "base_url", None):
        return args.base_url.rstrip("/")
    effective_env = env if env is not None else load_env()
    return effective_env.get("ARC_BENCH_WEB_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _config(
    args: argparse.Namespace,
    env: dict[str, str] | None = None,
) -> tuple[dict[str, str], SubmitConfig]:
    effective_env = env if env is not None else load_env()
    config = SubmitConfig.from_env(effective_env)
    if getattr(args, "base_url", None):
        config.base_url = args.base_url.rstrip("/")
    return effective_env, config


def _default_record_dir(env: dict[str, str]) -> Path:
    configured = env.get("ARC_BENCH_RECORD_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / ".arcbench" / "runs" / "submissions").resolve()


def _find_lab_root(explicit: str | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("ARCBENCH_LAB_ROOT"):
        candidates.append(Path(os.environ["ARCBENCH_LAB_ROOT"]))
    candidates.extend([Path.cwd(), PROJECT_ROOT])
    for candidate in candidates:
        root = candidate.expanduser().resolve()
        if (
            root / "evolution" / "adapters" / "arcbench" / "submission"
        ).is_dir() and (root / "scripts" / "replay.py").is_file():
            return root
    return None


def _require_lab_root(explicit: str | None = None) -> Path:
    root = _find_lab_root(explicit)
    if root is None:
        raise CliError(
            "harness lab checkout not found; pass --lab-root or set ARCBENCH_LAB_ROOT"
        )
    return root


def _write_pack(
    client: OfficialClient,
    out: Path,
    task_id: str,
) -> dict[str, Any]:
    payload = client.get_test_pack(task_id)
    files = payload.get("files") or []
    if not files:
        raise CliError(f"no test files returned for {task_id}")
    out.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, Any]] = []
    for entry in files:
        relative = entry.get("path") or ""
        content = entry.get("content") or ""
        if not relative or ".." in Path(relative).parts:
            continue
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(
            {"path": relative, "bytes": len(content.encode("utf-8"))}
        )
    manifest = {
        "label": "competition-official",
        "source": (
            f"{client.config.base_url}/api/requirements/{task_id}/tests"
            "?catalog=competition"
        ),
        "task_id": task_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(written),
        "files": written,
    }
    (out / "fetch-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def cmd_session(args: argparse.Namespace) -> int:
    env_path = (
        Path(args.env).expanduser().resolve()
        if args.env
        else (
            Path(os.environ["ARCBENCH_ENV_FILE"]).expanduser().resolve()
            if os.environ.get("ARCBENCH_ENV_FILE")
            else default_env_path().resolve()
        )
    )
    path, length = capture_chrome_cookie(args.profile, env_path)
    log(f"wrote ARC_BENCH_SESSION_COOKIE to {path} ({length} chars, mode 600)")
    log("the value is never printed; re-run after the session expires")
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    env, config = _config(args)
    client = OfficialClient(config)
    competitions = client.list_competitions()
    for competition in competitions:
        cid = competition.get("id", "-")
        print(
            f"{cid:26s} type={str(competition.get('type', '-')):6s} "
            f"tasks={competition.get('task_count')} tests={competition.get('total_tests')} "
            f"status={competition.get('status')}"
        )
        if not args.verbose:
            continue
        detail = client.get_competition(cid)
        for task in detail.get("tasks") or []:
            print(
                f"    {str(task.get('id', '-')):38s} tests={task.get('total_tests')} "
                f"modules={task.get('module_count')}"
            )
    print("\n`arcbench fetch --competition <id>` downloads a competition's test packs.")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    env, config = _config(args)
    client = OfficialClient(config)
    if args.out:
        root = Path(args.out).expanduser().resolve()
    else:
        lab_root = _find_lab_root()
        root = (
            lab_root / "testsets" / "official"
            if lab_root is not None
            else Path.cwd() / "testsets" / "official"
        ).resolve()
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
        entry = _write_pack(client, root / task_id, str(task_id))
        entry.update(
            {
                "title": task.get("title"),
                "total_tests": task.get("total_tests"),
            }
        )
        index.append(entry)
        log(
            f"{task_id}: {entry['file_count']} files, "
            f"{task.get('total_tests')} tests"
        )
    (root / "index.json").write_text(
        json.dumps(
            {
                "competition_id": args.competition,
                "title": detail.get("title"),
                "total_tests": detail.get("total_tests"),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "tasks": index,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    log(f"wrote {len(index)} packs to {root}")
    return 0


def cmd_package(args: argparse.Namespace) -> int:
    lab_root = _require_lab_root(args.lab_root)
    submission_dir = (
        lab_root / "evolution" / "adapters" / "arcbench" / "submission"
    )
    vendored_pi = lab_root / "evolution" / "vendor" / "pi-coding-agent-node20.tgz"
    template_dir = (
        lab_root
        / "upstream"
        / "arc-template"
        / "templates"
        / "web-react-express"
    )
    missing = [
        name for name in ROOT_FILES if not (submission_dir / name).is_file()
    ]
    if missing:
        raise CliError(f"missing submission files: {', '.join(missing)}")
    if not vendored_pi.is_file():
        raise CliError(
            f"missing vendored pi tarball: {vendored_pi}; "
            "run scripts/vendor-node20-pi.sh first"
        )
    if not template_dir.is_dir():
        raise CliError(
            f"missing starter template: {template_dir}; "
            "run scripts/fetch-upstream.py first"
        )

    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ROOT_FILES:
            archive.write(submission_dir / name, name)
        archive.write(vendored_pi, "vendor/pi-coding-agent-node20.tgz")
        for path in sorted(template_dir.rglob("*")):
            relative = path.relative_to(template_dir)
            if not path.is_file() or TEMPLATE_EXCLUDED.intersection(relative.parts):
                continue
            archive.write(path, (Path("starter-template") / relative).as_posix())

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    log(f"{out}")
    log(f"bytes={out.stat().st_size} sha256={digest}")
    return 0


def _record_path(record_dir: Path, competition: str, task: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return record_dir / f"{stamp}-{competition}-{task}.json"


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
    lines = []
    for line in text[newline + 1 : finish].splitlines():
        line = re.sub(
            r"^\[[^\]]+\] \[runner\] generation-agent\.stdout \| ?",
            "",
            line,
        )
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def cmd_submit(args: argparse.Namespace) -> int:
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
        raise CliError(
            "ARC_BENCH_SESSION_COOKIE is not configured; run `arcbench session`"
        )
    if not config.api_key:
        raise CliError(
            "ARC_BENCH_API_KEY is not configured; put it in the env file "
            "or export it"
        )

    client = OfficialClient(config)
    login = client.check_login()
    log(f"login: {json.dumps(login, ensure_ascii=False)}")
    if not login.get("logged_in"):
        return 2

    tasks = client.list_tasks(args.competition)
    task_ids = [str(task.get("id")) for task in tasks]
    if task_ids and args.task not in task_ids:
        raise CliError(
            f"task {args.task!r} is not in {args.competition}: {task_ids}"
        )

    log(f"package sha256={package['sha256'][:12]}... bytes={package['bytes']}")
    log(
        f"competition={args.competition} task={args.task} "
        f"model={model} name={name}"
    )
    if args.dry_run:
        log("dry-run: nothing submitted")
        return 0

    enforce_budget(record_dir, config)

    if args.submission_id:
        submission_id = args.submission_id
        log(f"reusing submission {submission_id}")
    else:
        submission = client.create_submission(
            Path(args.package).expanduser().resolve(),
            args.competition,
            name,
            model,
        )
        submission_id = submission.get("id") or submission.get("submission_id")
        if not submission_id:
            raise CliError(
                "submission response had no id: "
                + json.dumps(submission, ensure_ascii=False)[:300]
            )
        log(f"submission created: {submission_id}")

    run = client.create_run(str(submission_id), args.task)
    run_id = run.get("id") or run.get("run_id")
    if not run_id:
        raise CliError(
            "run response had no id: "
            + json.dumps(run, ensure_ascii=False)[:300]
        )
    log(f"run started: {run_id}  https://arc-bench.com/runs/{run_id}")

    seen: set[str] = set()

    def tick(current: dict[str, Any]) -> None:
        line = progress_line(current)
        if line not in seen:
            seen.add(line)
            log(f"  {line}")

    if not args.no_wait:
        run = client.poll_run(
            str(run_id), args.poll_interval, args.poll_timeout, on_tick=tick
        )

    metrics = summarize_run(run)
    record = {
        "record_type": "competition-official",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "submission_name": name,
        "submission_id": submission_id,
        "competition": args.competition,
        "task": args.task,
        "model": model,
        "package": package,
        "local_run_id": args.local_run_id,
        "local_manifest": args.local_manifest,
        "metrics": metrics,
        "run_url": f"https://arc-bench.com/runs/{run_id}",
    }
    path = _record_path(record_dir, args.competition, args.task)
    write_record(path, record)
    log(f"result: {json.dumps(metrics, ensure_ascii=False)}")
    log(f"record: {path}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    env, config = _config(args)
    if not config.session_cookie:
        raise CliError(
            "ARC_BENCH_SESSION_COOKIE is not configured; run `arcbench session`"
        )
    client = OfficialClient(config)
    payload = client.get_run_logs(args.run_id)
    out_dir = (
        Path(args.out).expanduser().resolve()
        if args.out
        else (Path.cwd() / ".arcbench" / "runs" / "logs" / args.run_id).resolve()
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_path = out_dir / f"{args.run_id}-logs.json"
    logs_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log(f"logs: {logs_path}")

    text = "\n".join(
        str(value)
        for key in ("stdout", "stderr", "console", "events")
        if (value := payload.get(key))
    )
    requirements = extract_marked_block(text)
    if requirements is None:
        log("no REQUIREMENTS-BEGIN/END markers found")
        return 0
    requirements_path = out_dir / "requirements.md"
    requirements_path.write_text(requirements, encoding="utf-8")
    log(f"captured requirements ({len(requirements)} chars): {requirements_path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    env, config = _config(args)
    if not config.session_cookie:
        raise CliError(
            "ARC_BENCH_SESSION_COOKIE is not configured; run `arcbench session`"
        )
    client = OfficialClient(config)

    if not args.run_id:
        for run in client.list_runs(args.limit):
            print(
                f"{run.get('id')}  {str(run.get('status')):9s} "
                f"{run.get('competition_id')}/{run.get('requirement_id')}  "
                f"{run.get('display_name')}"
            )
        return 0

    if args.wait:
        run = client.poll_run(
            args.run_id,
            args.poll_interval,
            args.poll_timeout,
            on_tick=lambda current: log(f"  {progress_line(current)}"),
        )
    else:
        run = client.get_run(args.run_id)
    metrics = summarize_run(run)
    log(f"result: {json.dumps(metrics, ensure_ascii=False)}")
    if args.record:
        if not args.competition or not args.task:
            raise CliError("--competition and --task are required with --record")
        record_dir = (
            Path(args.record_dir).expanduser().resolve()
            if args.record_dir
            else _default_record_dir(env)
        )
        path = _record_path(record_dir, args.competition, args.task)
        write_record(
            path,
            {
                "record_type": "competition-official",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "submission_id": run.get("submission_id"),
                "competition": args.competition,
                "task": args.task,
                "metrics": metrics,
                "run_url": f"https://arc-bench.com/runs/{args.run_id}",
            },
        )
        log(f"record: {path}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    lab_root = _require_lab_root(args.lab_root)
    replay_script = lab_root / "scripts" / "replay.py"
    command = [
        sys.executable,
        str(replay_script),
        "--tests",
        str(Path(args.tests).expanduser().resolve()),
        "--out",
        str(Path(args.out).expanduser().resolve()),
    ]
    if args.manifest:
        command += ["--manifest", str(Path(args.manifest).expanduser().resolve())]
    elif args.app:
        command += ["--app", str(Path(args.app).expanduser().resolve())]
    else:
        raise CliError("--manifest or --app is required")
    if args.label:
        command += ["--label", args.label]
    if args.probe_only:
        command += ["--probe-only"]
    log("exec: " + " ".join(command))
    return subprocess.run(command, cwd=lab_root).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arcbench",
        description=(
            "Discover ARC-Bench tasks, fetch official test packs, submit a "
            "candidate harness, poll runs, and replay archived apps."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="ARC-Bench web base URL; overrides ARC_BENCH_WEB_BASE_URL",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("session", help="capture the local ARC-Bench session into .env")
    p.add_argument("--profile", default="Default", help="Chrome profile name")
    p.add_argument("--env", help="env file to update; defaults to ./.env")
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("tasks", help="list competitions and optionally their tasks")
    p.add_argument("-v", "--verbose", action="store_true", help="also list each task")
    p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("fetch", help="download a competition's official test packs")
    p.add_argument("--competition", required=True)
    p.add_argument("--out", help="output root; defaults to ./testsets/official")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("package", help="build the harness submission ZIP")
    p.add_argument(
        "--lab-root",
        help="path to arcbench-harness-lab; defaults to ARCBENCH_LAB_ROOT or cwd",
    )
    p.add_argument(
        "--out",
        default="dist/pi-harness-agent.zip",
        help="output ZIP path",
    )
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("submit", help="upload, run one task, and record the result")
    p.add_argument("--package", required=True)
    p.add_argument("--competition", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--name")
    p.add_argument("--model")
    p.add_argument("--submission-id", help="reuse an existing submission")
    p.add_argument("--record-dir", help="where JSON result records are written")
    p.add_argument("--local-run-id")
    p.add_argument("--local-manifest")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--poll-interval", type=float, default=20.0)
    p.add_argument("--poll-timeout", type=float, default=3600.0)
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("status", help="list runs, or poll one run")
    p.add_argument("--run-id")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--wait", action="store_true")
    p.add_argument("--record", action="store_true")
    p.add_argument("--competition")
    p.add_argument("--task")
    p.add_argument("--record-dir", help="where JSON result records are written")
    p.add_argument("--poll-interval", type=float, default=20.0)
    p.add_argument("--poll-timeout", type=float, default=3600.0)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("logs", help="download one run's logs and capture marked requirements")
    p.add_argument("--run-id", required=True)
    p.add_argument("--out", help="output directory")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("replay", help="re-score an archived app against any suite")
    p.add_argument("--lab-root")
    p.add_argument("--manifest")
    p.add_argument("--app")
    p.add_argument("--tests", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--label", default="")
    p.add_argument("--probe-only", action="store_true")
    p.set_defaults(func=cmd_replay)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    except BrokenPipeError:
        return 0
    except (CliError, RuntimeError, ValueError) as error:
        log(str(error))
        return 1
    except urllib.error.URLError as error:
        log(f"network error: {error.reason}")
        return 1
    except Exception as error:
        if os.environ.get("ARCBENCH_DEBUG"):
            raise
        log(f"unexpected error: {type(error).__name__}: {error}")
        return 1
