from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from arcbench_cli.client import (
    ApiError,
    OfficialClient,
    RunQueueTimeoutError,
    RunStartError,
    SubmitConfig,
    build_multipart,
    load_env,
    parse_retry_after,
    sanitize,
    normalize_task_filter,
    parse_cookie_header,
    summarize_run,
    validate_package,
    write_record,
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
            "tokens": 123,
            "nested": {"session_cookie": "cookie", "safe": "kept"},
        }
        self.assertEqual(
            sanitize(payload),
            {
                "api_key": "***",
                "tokens": 123,
                "nested": {"session_cookie": "***", "safe": "kept"},
            },
        )

    def test_write_record_keeps_token_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.json"
            write_record(
                path,
                {
                    "api_key": "secret",
                    "metrics": {"tokens": 123, "token_count": 123},
                },
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["api_key"], "***")
            self.assertEqual(saved["metrics"]["tokens"], 123)
            self.assertEqual(saved["metrics"]["token_count"], 123)

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
        self.assertEqual(config.queue_timeout_seconds, 3600.0)

    def test_create_run_only_persists_pending_run(self) -> None:
        class FakeClient(OfficialClient):
            def __init__(self) -> None:
                super().__init__(SubmitConfig())
                self.calls: list[tuple[str, str]] = []

            def _request(self, method, url, data=None, content_type=None):
                self.calls.append((method, url))
                if url.endswith("/api/runs"):
                    return 200, {"run": {"id": "run-1", "status": "PENDING"}}
                raise AssertionError(url)

        client = FakeClient()
        run = client.create_run("submission-1", "smoke--counter")
        self.assertEqual(run["status"], "PENDING")
        self.assertEqual(
            client.calls,
            [("POST", "https://arc-bench.com/api/runs")],
        )


class QueueWaitTests(unittest.TestCase):
    def test_retries_429_then_succeeds(self) -> None:
        class FakeClient(OfficialClient):
            def __init__(self) -> None:
                super().__init__(SubmitConfig())
                self.start_attempts = 0

            def start_run(self, run_id: str) -> dict[str, object]:
                self.start_attempts += 1
                if self.start_attempts < 3:
                    raise RunStartError(
                        run_id,
                        429,
                        {"detail": "capacity full"},
                        {"retry-after": "0"},
                    )
                return {"id": run_id, "status": "QUEUED"}

        client = FakeClient()
        with patch("arcbench_cli.client.time.sleep"):
            run = client.start_run_with_queue_wait("run-1", timeout_seconds=5)
        self.assertEqual(client.start_attempts, 3)
        self.assertEqual(run["status"], "QUEUED")

    def test_parse_retry_after_seconds_and_default(self) -> None:
        self.assertEqual(parse_retry_after({"retry-after": "45"}), 45.0)
        self.assertEqual(parse_retry_after({"retry-after": "0"}), 0.0)
        self.assertEqual(parse_retry_after({}), 30.0)
        self.assertEqual(parse_retry_after({"retry-after": "soon"}), 30.0)

    def test_timeout_reports_run_id_and_recovery(self) -> None:
        class FakeClient(OfficialClient):
            def __init__(self) -> None:
                super().__init__(SubmitConfig())
                self.start_attempts = 0

            def start_run(self, run_id: str) -> dict[str, object]:
                self.start_attempts += 1
                raise RunStartError(
                    run_id,
                    429,
                    {"detail": "capacity full"},
                    {"retry-after": "30"},
                )

            def get_run(self, run_id: str) -> dict[str, str]:
                return {"id": run_id, "status": "PENDING"}

        client = FakeClient()
        with self.assertRaisesRegex(RunQueueTimeoutError, "arcbench start run-1"):
            client.start_run_with_queue_wait("run-1", timeout_seconds=0)
        self.assertEqual(client.start_attempts, 1)

    def test_non_429_is_not_retried(self) -> None:
        class FakeClient(OfficialClient):
            def __init__(self) -> None:
                super().__init__(SubmitConfig())
                self.start_attempts = 0

            def start_run(self, run_id: str) -> dict[str, object]:
                self.start_attempts += 1
                raise RunStartError(run_id, 409, {"detail": "already started"})

        client = FakeClient()
        with patch("arcbench_cli.client.time.sleep") as sleep:
            with self.assertRaises(RunStartError):
                client.start_run_with_queue_wait("run-1", timeout_seconds=5)
        self.assertEqual(client.start_attempts, 1)
        sleep.assert_not_called()


class MergedHelperTests(unittest.TestCase):
    def test_task_filter_uses_the_id_suffix_like_the_website(self) -> None:
        self.assertEqual(normalize_task_filter("ticket-booking--ticket-booking"), "ticket-booking")
        self.assertEqual(normalize_task_filter("ticket-booking"), "ticket-booking")
        self.assertIsNone(normalize_task_filter("all"))
        self.assertIsNone(normalize_task_filter(None))

    def test_cookie_header_is_parsed_into_pairs(self) -> None:
        self.assertEqual(
            parse_cookie_header("arcbench_session=abc; arc_api_sticky=route-1"),
            [("arcbench_session", "abc"), ("arc_api_sticky", "route-1")],
        )
        self.assertEqual(parse_cookie_header(""), [])

    def test_sanitize_replaces_known_secret_values_in_free_text(self) -> None:
        cleaned = sanitize({"console": "used key ak-live-value here"}, ("ak-live-value",))
        self.assertEqual(cleaned["console"], "used key *** here")

    def test_sanitize_masks_a_meter_baseline_identifier(self) -> None:
        cleaned = sanitize({"console": "Meter baseline captured for access key abcdef123"})
        self.assertNotIn("abcdef123", cleaned["console"])

    def test_summarize_run_reports_cost_with_its_declared_currency(self) -> None:
        summary = summarize_run({"id": "r", "token_cost_usd": 0.02, "token_cost_currency": "CNY"})
        self.assertEqual(summary["cost"], {"amount": 0.02, "currency": "CNY"})

    def test_api_error_details_describe_certainty(self) -> None:
        error = ApiError("boom", status=503, method="POST", path="/runs", uncertain=True)
        self.assertTrue(error.uncertain)
        self.assertEqual(error.status, 503)
        self.assertFalse(error.transport)

    def test_client_seeds_the_configured_cookie_into_its_jar(self) -> None:
        client = OfficialClient(
            SubmitConfig(session_cookie="arcbench_session=abc; arc_api_sticky=route-1")
        )
        self.assertEqual(
            sorted(cookie.name for cookie in client.cookies),
            ["arc_api_sticky", "arcbench_session"],
        )
        # The session value is registered so it can never be echoed back out.
        self.assertIn("abc", client.known_secrets)

    def test_client_refuses_a_non_http_base_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "http"):
            OfficialClient(SubmitConfig(base_url="ftp://example.com"))

    def test_poll_run_rejects_a_non_positive_interval(self) -> None:
        client = OfficialClient(SubmitConfig())
        with self.assertRaisesRegex(ValueError, "positive"):
            client.poll_run("run-1", interval=0, timeout=5)


if __name__ == "__main__":
    unittest.main()
