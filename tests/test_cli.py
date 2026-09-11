from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arcbench_cli import cli as cli_module
from arcbench_cli.cli import build_parser, cmd_submit, extract_marked_block


class CliParserTests(unittest.TestCase):
    def test_submit_requires_package_competition_and_task(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "submit",
                "--package",
                "candidate.zip",
                "--competition",
                "smoke",
                "--task",
                "smoke--counter",
                "--dry-run",
            ]
        )
        self.assertEqual(args.func.__name__, "cmd_submit")
        self.assertTrue(args.dry_run)

    def test_package_has_configurable_lab_root(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["package", "--lab-root", "/tmp/lab", "--out", "/tmp/out.zip"]
        )
        self.assertEqual(args.lab_root, "/tmp/lab")
        self.assertEqual(args.out, "/tmp/out.zip")

    def test_submit_and_start_accept_queue_timeout(self) -> None:
        parser = build_parser()
        submit_args = parser.parse_args(
            [
                "submit",
                "--package",
                "candidate.zip",
                "--competition",
                "smoke",
                "--task",
                "smoke--counter",
                "--queue-timeout",
                "123",
                "--dry-run",
            ]
        )
        start_args = parser.parse_args(["start", "--run-id", "run-1", "--queue-timeout", "456"])
        self.assertEqual(submit_args.queue_timeout, 123.0)
        self.assertEqual(start_args.queue_timeout, 456.0)

    def test_fetch_accepts_output_root(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["fetch", "--competition", "smoke", "--out", "/tmp/tests"]
        )
        self.assertEqual(args.out, "/tmp/tests")

    def test_extract_marked_block_removes_runner_prefixes(self) -> None:
        text = (
            "[time] [runner] generation-agent.stdout | REQUIREMENTS-BEGIN (5 chars)\n"
            "[time] [runner] generation-agent.stdout | hello\n"
            "[time] [runner] generation-agent.stdout | world\n"
            "[time] [runner] generation-agent.stdout | REQUIREMENTS-END\n"
        )
        self.assertEqual(extract_marked_block(text), "hello\nworld\n")

    def test_submit_creates_run_once_then_waits_for_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "candidate.zip"
            package.write_bytes(b"zip-data")
            args = build_parser().parse_args(
                [
                    "submit",
                    "--package",
                    str(package),
                    "--competition",
                    "smoke",
                    "--task",
                    "smoke--counter",
                    "--record-dir",
                    tmp,
                    "--no-wait",
                ]
            )

            class FakeClient:
                def __init__(self, config: object) -> None:
                    self.config = config
                    self.calls: list[str] = []

                def check_login(self) -> dict[str, object]:
                    self.calls.append("check_login")
                    return {"logged_in": True}

                def list_tasks(self, competition_id: str) -> list[object]:
                    self.calls.append("list_tasks")
                    return []

                def create_submission(
                    self,
                    package: Path,
                    competition_id: str,
                    name: str,
                    model: str,
                ) -> dict[str, str]:
                    self.calls.append("create_submission")
                    return {"id": "submission-1"}

                def create_run(
                    self,
                    submission_id: str,
                    requirement_id: str,
                ) -> dict[str, str]:
                    self.calls.append("create_run")
                    return {"id": "run-1", "status": "PENDING"}

                def start_run_with_queue_wait(
                    self,
                    run_id: str,
                    timeout_seconds: float,
                    on_wait: object,
                ) -> dict[str, object]:
                    self.calls.append("start_run_with_queue_wait")
                    return {
                        "id": run_id,
                        "status": "PASSED",
                        "token_count": 123,
                    }

            fake_client = FakeClient(object())
            with (
                patch.object(
                    cli_module,
                    "validate_package",
                    return_value={
                        "path": str(package),
                        "bytes": 0,
                        "sha256": "0" * 64,
                    },
                ),
                patch.object(
                    cli_module,
                    "load_env",
                    return_value={
                        "ARC_BENCH_SESSION_COOKIE": "cookie",
                        "ARC_BENCH_API_KEY": "secret",
                    },
                ),
                patch.object(cli_module, "OfficialClient", return_value=fake_client),
                patch.object(cli_module, "enforce_budget"),
            ):
                result = cmd_submit(args)

            self.assertEqual(result, 0)
            self.assertEqual(fake_client.calls.count("create_submission"), 1)
            self.assertEqual(fake_client.calls.count("create_run"), 1)
            self.assertEqual(fake_client.calls.count("start_run_with_queue_wait"), 1)
            self.assertEqual(fake_client.calls, [
                "check_login",
                "list_tasks",
                "create_submission",
                "create_run",
                "start_run_with_queue_wait",
            ])


if __name__ == "__main__":
    unittest.main()
