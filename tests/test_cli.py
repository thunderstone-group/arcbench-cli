"""Parser and command-flow tests that do not touch the network."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from arcbench_cli import cli as cli_module
from arcbench_cli.cli import (
    build_parser,
    cmd_package,
    cmd_submit,
    extract_marked_block,
    main,
    resolve_run_id,
)

COMMANDS = {
    "session",
    "whoami",
    "balance",
    "tasks",
    "competitions",
    "fetch",
    "submissions",
    "runs",
    "leaderboard",
    "package",
    "upload",
    "run",
    "start",
    "cancel",
    "status",
    "wait",
    "logs",
    "download",
    "archive",
    "source",
    "submit",
}


class CliParserTests(unittest.TestCase):
    def test_every_documented_command_is_reachable(self) -> None:
        parser = build_parser()
        actions = [
            action for action in parser._subparsers._actions if hasattr(action, "choices") and action.choices
        ]
        self.assertEqual(set(actions[-1].choices), COMMANDS)

    def test_submit_requires_package_competition_and_task(self) -> None:
        args = build_parser().parse_args(
            ["submit", "--package", "candidate.zip", "--competition", "smoke",
             "--task", "smoke--counter", "--dry-run"]
        )
        self.assertEqual(args.func.__name__, "cmd_submit")
        self.assertTrue(args.dry_run)

    def test_package_takes_a_source_directory_not_a_lab_checkout(self) -> None:
        args = build_parser().parse_args(["package", "--from", "/tmp/agent", "--out", "/tmp/out.zip"])
        self.assertEqual(args.source, "/tmp/agent")
        self.assertEqual(args.out, "/tmp/out.zip")

    def test_submit_and_start_accept_queue_timeout(self) -> None:
        submit_args = build_parser().parse_args(
            ["submit", "--package", "candidate.zip", "--competition", "smoke",
             "--task", "smoke--counter", "--queue-timeout", "123", "--dry-run"]
        )
        start_args = build_parser().parse_args(["start", "--run-id", "run-1", "--queue-timeout", "456"])
        self.assertEqual(submit_args.queue_timeout, 123.0)
        self.assertEqual(start_args.queue_timeout, 456.0)

    def test_run_id_is_accepted_positionally_or_by_flag(self) -> None:
        self.assertEqual(build_parser().parse_args(["start", "run-1"]).run, "run-1")
        self.assertEqual(resolve_run_id(build_parser().parse_args(["start", "--run-id", "run-1"])).run, "run-1")
        self.assertEqual(build_parser().parse_args(["status"]).run, None)

    def test_tasks_takes_an_optional_competition(self) -> None:
        self.assertIsNone(build_parser().parse_args(["tasks"]).competition)
        self.assertEqual(build_parser().parse_args(["tasks", "smoke"]).competition, "smoke")
        self.assertIsNone(build_parser().parse_args(["competitions"]).competition)

    def test_fetch_accepts_output_root(self) -> None:
        args = build_parser().parse_args(["fetch", "--competition", "smoke", "--out", "/tmp/tests"])
        self.assertEqual(args.out, "/tmp/tests")

    def test_start_without_a_run_id_fails_before_any_request(self) -> None:
        self.assertEqual(main(["start"]), 1)


class MarkedBlockTests(unittest.TestCase):
    def test_extract_marked_block_removes_runner_prefixes(self) -> None:
        text = (
            "[time] [runner] generation-agent.stdout | REQUIREMENTS-BEGIN (5 chars)\n"
            "[time] [runner] generation-agent.stdout | hello\n"
            "[time] [runner] generation-agent.stdout | world\n"
            "[time] [runner] generation-agent.stdout | REQUIREMENTS-END\n"
        )
        self.assertEqual(extract_marked_block(text), "hello\nworld\n")

    def test_missing_markers_return_none(self) -> None:
        self.assertIsNone(extract_marked_block("nothing marked here"))
        self.assertIsNone(extract_marked_block("REQUIREMENTS-BEGIN but never closed"))


class PackageTests(unittest.TestCase):
    def _args(self, source: Path, out: Path):
        return build_parser().parse_args(["package", "--from", str(source), "--out", str(out)])

    def test_package_builds_a_valid_archive_from_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "agent"
            (source / "lib").mkdir(parents=True)
            (source / "main.py").write_text("print('hi')\n", encoding="utf-8")
            (source / "lib" / "helper.py").write_text("value = 1\n", encoding="utf-8")
            (source / "node_modules").mkdir()
            (source / "node_modules" / "junk.js").write_text("ignored\n", encoding="utf-8")
            out = Path(tmp) / "dist" / "agent.zip"
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cmd_package(self._args(source, out)), 0)
            with zipfile.ZipFile(out) as archive:
                self.assertEqual(sorted(archive.namelist()), ["lib/helper.py", "main.py"])

    def test_package_refuses_a_directory_without_an_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "agent"
            source.mkdir()
            (source / "README.md").write_text("no entrypoint\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "no root entrypoint"):
                cmd_package(self._args(source, Path(tmp) / "out.zip"))

    def test_package_refuses_to_overwrite_an_existing_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "agent"
            source.mkdir()
            (source / "main.py").write_text("print('hi')\n", encoding="utf-8")
            out = Path(tmp) / "out.zip"
            out.write_bytes(b"existing")
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                cmd_package(self._args(source, out))


class FakeClient:
    """Records the order of platform calls a submit performs."""

    def __init__(self, config: object) -> None:
        self.config = config
        self.calls: list[str] = []
        self.known_secrets: list[str] = []

    def check_login(self) -> dict[str, object]:
        self.calls.append("check_login")
        return {"logged_in": True}

    def list_tasks(self, competition_id: str) -> list[object]:
        self.calls.append("list_tasks")
        return []

    def upload(self, package, competition_id, name, model, runtime="python", api_key=None):
        self.calls.append("upload")
        return {"submission": {"id": "submission-1"}, "archive_verified": True,
                "uploaded_sha256": "0" * 64}

    def create_run(self, submission_id: str, requirement_id: str) -> dict[str, str]:
        self.calls.append("create_run")
        return {"id": "run-1", "status": "PENDING"}

    def start_run_with_queue_wait(self, run_id, timeout_seconds, on_wait=None):
        self.calls.append("start_run_with_queue_wait")
        return {"id": run_id, "status": "PASSED", "token_count": 123}


class SubmitFlowTests(unittest.TestCase):
    def _run_submit(self, tmp: str, extra: list[str]) -> tuple[int, FakeClient, Path]:
        package = Path(tmp) / "candidate.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("main.py", "print('agent')\n")
        args = build_parser().parse_args(
            ["submit", "--package", str(package), "--competition", "smoke",
             "--task", "smoke--counter", "--record-dir", tmp, *extra]
        )
        client = FakeClient(object())
        with (
            patch.object(cli_module, "load_env", return_value={
                "ARC_BENCH_SESSION_COOKIE": "arcbench_session=cookie",
                "ARC_BENCH_API_KEY": "secret",
            }),
            patch.object(cli_module, "OfficialClient", return_value=client),
            patch.object(cli_module, "enforce_budget"),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                code = cmd_submit(args)
        return code, client, package

    def test_submit_uploads_once_creates_one_run_and_starts_it_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, client, _ = self._run_submit(tmp, ["--no-wait"])
            self.assertEqual(code, 0)
            self.assertEqual(
                client.calls,
                ["check_login", "list_tasks", "upload", "create_run", "start_run_with_queue_wait"],
            )

    def test_submit_writes_a_redacted_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._run_submit(tmp, ["--no-wait"])
            records = list(Path(tmp).glob("*.json"))
            self.assertEqual(len(records), 1)
            saved = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["submission_id"], "submission-1")
            self.assertTrue(saved["archive_verified"])
            self.assertEqual(saved["metrics"]["tokens"], 123)

    def test_dry_run_contacts_nothing_that_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, client, _ = self._run_submit(tmp, ["--dry-run"])
            self.assertEqual(code, 0)
            self.assertEqual(client.calls, ["check_login", "list_tasks"])
            self.assertEqual(list(Path(tmp).glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
