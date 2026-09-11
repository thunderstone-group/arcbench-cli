from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from arcbench_cli.client import (
    OfficialClient,
    SubmitConfig,
    build_multipart,
    load_env,
    sanitize,
    summarize_run,
    validate_package,
)


class ClientTests(unittest.TestCase):
    def test_validate_package_accepts_python_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "candidate.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("main.py", "print('ok')\n")
                archive.writestr("requirements.txt", "")
            result = validate_package(package)
            self.assertEqual(result["bytes"], package.stat().st_size)
            self.assertEqual(len(result["sha256"]), 64)

    def test_validate_package_rejects_missing_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "candidate.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("README.md", "not a submission\n")
            with self.assertRaisesRegex(ValueError, "root entrypoint"):
                validate_package(package)

    def test_build_multipart_contains_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "candidate.zip"
            package.write_bytes(b"zip-data")
            body, content_type = build_multipart(
                {"competition_id": "smoke"}, "file", package, boundary="test-boundary"
            )
            self.assertEqual(content_type, "multipart/form-data; boundary=test-boundary")
            self.assertIn(b'name="competition_id"', body)
            self.assertIn(b'filename="candidate.zip"', body)
            self.assertIn(b"zip-data", body)

    def test_load_env_prefers_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("ARC_BENCH_MODEL=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"ARC_BENCH_MODEL": "from-process"}, clear=False):
                values = load_env(env_file)
            self.assertEqual(values["ARC_BENCH_MODEL"], "from-process")

    def test_sanitize_removes_credentials(self) -> None:
        payload = {
            "api_key": "secret",
            "nested": {"session_cookie": "cookie", "safe": "kept"},
        }
        self.assertEqual(
            sanitize(payload),
            {
                "api_key": "***",
                "nested": {"session_cookie": "***", "safe": "kept"},
            },
        )

    def test_summarize_run_keeps_metric_triple(self) -> None:
        metrics = summarize_run(
            {
                "id": "run-1",
                "status": "PASSED",
                "passed_count": 10,
                "failed_count": 0,
                "test_pass_rate": 1.0,
                "token_count": 123,
                "run_duration_seconds": 45,
            }
        )
        self.assertEqual(metrics["run_id"], "run-1")
        self.assertEqual(metrics["pass_rate"], 1.0)
        self.assertEqual(metrics["tokens"], 123)
        self.assertEqual(metrics["duration_seconds"], 45)

    def test_config_defaults_are_safe(self) -> None:
        config = SubmitConfig.from_env({})
        self.assertEqual(config.max_submissions, 4)
        self.assertEqual(config.min_interval_seconds, 30.0)

    def test_create_run_starts_pending_run(self) -> None:
        class FakeClient(OfficialClient):
            def __init__(self) -> None:
                super().__init__(SubmitConfig())
                self.calls: list[tuple[str, str]] = []

            def _request(self, method, url, data=None, content_type=None):
                self.calls.append((method, url))
                if url.endswith("/api/runs"):
                    return 200, {"run": {"id": "run-1", "status": "PENDING"}}
                if url.endswith("/api/runs/run-1/start"):
                    return 200, {"id": "run-1", "status": "QUEUED"}
                raise AssertionError(url)

        client = FakeClient()
        run = client.create_run("submission-1", "smoke--counter")
        self.assertEqual(run["status"], "QUEUED")
        self.assertEqual(
            client.calls,
            [
                ("POST", "https://arc-bench.com/api/runs"),
                ("POST", "https://arc-bench.com/api/runs/run-1/start"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
