"""Contract tests against a synthetic ARC-Bench server.

The handler below mirrors the platform's observed request and response shapes,
including the behaviours that are easy to get wrong: route-affinity cookies,
nested write responses, truncated bodies, and a cancellation that reports an
error after it has already taken effect. Passing these does not establish that
a live submission works; it establishes that the client encodes, recovers and
redacts the way the platform requires.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from arcbench_cli.client import (
    ApiError,
    OfficialClient,
    SubmitConfig,
    log_summary,
    summarize_run,
)
from arcbench_cli.cli import main

SESSION_VALUE = "synthetic-session-secret"
MODEL_KEY = "synthetic-model-secret"


class Website:
    """A minimal stand-in for the platform's HTTP surface."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.archive: bytes | None = None
        self.download_failure = False
        self.create_failure = False
        self.start_failure = False
        self.reject_auth = False
        self.mismatch = False
        self.redirect: str | None = None
        self.run_states = ["PASSED"]
        self.truncate_responses = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def serve(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                parts = {}
                if raw:
                    message = BytesParser(policy=default).parsebytes(
                        ("Content-Type: " + self.headers["Content-Type"] + "\r\n\r\n").encode() + raw
                    )
                    parts = {
                        part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                        for part in message.iter_parts()
                    }
                owner.requests.append(
                    {
                        "path": self.path,
                        "method": self.command,
                        "cookie": self.headers.get("Cookie"),
                        "parts": parts,
                    }
                )
                if owner.redirect:
                    self.send_response(302)
                    self.send_header("Location", owner.redirect)
                    self.end_headers()
                    return
                status, result, mime = 200, {}, "application/json"
                path = self.path
                if path == "/api/auth/me":
                    status, result = (
                        (401, {"detail": "not authenticated"})
                        if owner.reject_auth
                        else (200, {"user": {"username": "synthetic-user"}})
                    )
                elif path == "/api/user/me":
                    result = {"account": {"account_id": "synthetic-meter-account-id"}}
                elif path == "/api/user/balance":
                    result = {"balance": {"available_balance": "0.000000"}, "currency": "CNY"}
                elif path == "/api/user/freshness":
                    result = {"as_of": "2026-09-11T08:52:24Z", "pending_billing_events": 0}
                elif path == "/api/auth/access-key":
                    result = {"api_key": MODEL_KEY}
                elif path.startswith("/api/competitions/leaderboard?"):
                    result = [
                        {"username": "first-team", "avg_pass_rate": 100},
                        {"username": "second-team", "avg_pass_rate": 80},
                    ]
                elif path == "/api/submissions":
                    if self.command == "POST":
                        owner.archive = parts["file"]
                        result = {"submission": {"id": "submission-1", "api_key": MODEL_KEY}}
                    else:
                        result = [{"id": "submission-1", "competition_id": "sample"}]
                elif path == "/api/submissions/submission-1/archive":
                    if owner.download_failure:
                        status, result = 404, {"detail": "Submission archive is not available"}
                    else:
                        mime, result = "application/zip", b"wrong" if owner.mismatch else owner.archive
                elif path == "/api/runs":
                    status, result = (
                        (409, {"detail": "The submitted agent archive is no longer available"})
                        if owner.create_failure
                        else (200, {"run": {"id": "run-1", "status": "PENDING"}})
                    )
                elif path == "/api/runs/run-1/start":
                    status, result = (
                        (503, {"detail": "upstream unavailable"})
                        if owner.start_failure
                        else (200, {"id": "run-1", "status": "RUNNING"})
                    )
                elif path == "/api/runs/run-1/cancel":
                    owner.run_states = ["CANCELLED"]
                    status, result = 500, {"detail": "Internal Server Error"}
                elif path == "/api/runs/run-1":
                    result = {"id": "run-1", "status": owner.run_states[0]}
                    if len(owner.run_states) > 1:
                        owner.run_states.pop(0)
                elif path.startswith("/api/runs/run-1/logs"):
                    result = {"console": f"line-1\nkey {MODEL_KEY}\nline-3", "log_offset": 42}
                elif path.startswith("/api/runs/run-1/source?"):
                    result = {
                        "file_path": ".agent/trace.jsonl",
                        "kind": "file",
                        "content": f"safe\n{MODEL_KEY}",
                    }
                body = result if isinstance(result, bytes) else json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", mime)
                truncated = owner.truncate_responses > 0
                if truncated:
                    owner.truncate_responses -= 1
                self.send_header("Content-Length", str(len(body) + (100 if truncated else 0)))
                self.send_header("Set-Cookie", "arc_api_sticky=synthetic-route; Path=/; HttpOnly")
                self.end_headers()
                self.wfile.write(body)
                if truncated:
                    self.close_connection = True

            do_GET = do_POST = serve

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "Website":
        self.thread.start()
        return self

    def __exit__(self, *args) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class PlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="arcbench-test-")
        self.root = Path(self.temp.name)
        self.env_file = self.root / "test.env"
        self.package = self.root / "agent.zip"
        with zipfile.ZipFile(self.package, "w") as archive:
            archive.writestr("main.py", "print('synthetic test agent')")
        # Never let a real developer session leak into a contract test.
        self.clean_env = patch.dict(
            os.environ,
            {key: "" for key in os.environ if key.startswith("ARC_BENCH_")},
            clear=False,
        )
        self.clean_env.start()

    def tearDown(self) -> None:
        self.clean_env.stop()
        self.temp.cleanup()

    def config(self, server: Website, **overrides) -> SubmitConfig:
        values = {
            "base_url": server.origin,
            "session_cookie": f"arcbench_session={SESSION_VALUE}",
            "timeout_seconds": 5,
        }
        values.update(overrides)
        return SubmitConfig(**values)

    def client(self, server: Website, **overrides) -> OfficialClient:
        return OfficialClient(self.config(server, **overrides))

    def run_cli(self, server: Website, *argv: str) -> tuple[int, str]:
        self.env_file.write_text(
            f"ARC_BENCH_SESSION_COOKIE=arcbench_session={SESSION_VALUE}\n"
            f"ARC_BENCH_METER_COOKIE=arcbench_session={SESSION_VALUE}\n"
            f"ARC_BENCH_METER_BASE_URL={server.origin}\n",
            encoding="utf-8",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--base-url", server.origin, "--env-file", str(self.env_file), "--json", *argv])
        return code, output.getvalue()

    def upload(self, client: OfficialClient) -> dict:
        return client.upload(self.package, "sample", "合约测试", "deepseek-v4-flash")

    # --- request encoding and affinity ----------------------------------

    def test_upload_hash_run_sequence_and_cookie_affinity(self) -> None:
        with Website() as server:
            client = self.client(server)
            saved = self.upload(client)
            self.assertTrue(saved["archive_verified"])
            self.assertNotIn(MODEL_KEY, json.dumps(saved))
            client.create_run(saved["submission"]["id"], "sample--a")
            client.start_run("run-1")
            posts = [item for item in server.requests if item["method"] == "POST"]
            self.assertEqual(
                [item["path"] for item in posts],
                ["/api/submissions", "/api/runs", "/api/runs/run-1/start"],
            )
            self.assertEqual(posts[0]["parts"]["catalog"], b"competition")
            self.assertEqual(posts[0]["parts"]["agent_source"], b"upload")
            self.assertEqual(posts[0]["parts"]["display_name"].decode(), "合约测试")
            self.assertEqual(posts[0]["parts"]["file"], self.package.read_bytes())
            self.assertEqual(
                posts[1]["parts"],
                {"submission_id": b"submission-1", "requirement_id": b"sample--a"},
            )
            # The session cookie and the server's route-affinity cookie both ride along.
            self.assertIn("arcbench_session=" + SESSION_VALUE, posts[-1]["cookie"])
            self.assertIn("arc_api_sticky=synthetic-route", posts[-1]["cookie"])

    def test_upload_uses_account_key_without_exposing_it(self) -> None:
        with Website() as server:
            client = self.client(server)
            result = self.upload(client)
            fields = [item for item in server.requests if item["path"] == "/api/submissions"][0]
            self.assertEqual(fields["parts"]["api_key"], MODEL_KEY.encode())
            self.assertNotIn(MODEL_KEY, json.dumps(result))
            self.assertIn("/api/auth/access-key", [item["path"] for item in server.requests])

    def test_leaderboard_matches_task_suffix_and_keeps_server_order(self) -> None:
        with Website() as server:
            code, output = self.run_cli(
                server,
                "leaderboard",
                "--competition",
                "sample",
                "--task",
                "sample--a & b",
                "--team",
                "second-team",
            )
            self.assertEqual(code, 0)
            rows = json.loads(output)
            self.assertEqual(rows[0]["rank"], 2)
            self.assertEqual(rows[0]["username"], "second-team")
            self.assertEqual(
                server.requests[-1]["path"],
                "/api/competitions/leaderboard?track=all&competition_id=sample&task_id=a+%26+b",
            )

    # --- failure handling -----------------------------------------------

    def test_wait_recovers_one_incomplete_read_without_mutating_the_run(self) -> None:
        with Website() as server:
            server.truncate_responses = 1
            code, output = self.run_cli(server, "wait", "run-1", "--interval", "0.01", "--timeout", "5")
            self.assertEqual(code, 0)
            rows = [json.loads(line) for line in output.splitlines()]
            self.assertEqual(rows[0]["waiting_retry"], 1)
            self.assertTrue(rows[0]["transport_failure"])
            self.assertEqual(rows[-1]["status"], "PASSED")
            self.assertTrue(all(item["method"] == "GET" for item in server.requests))
            self.assertEqual(sum(item["path"] == "/api/runs/run-1" for item in server.requests), 2)

    def test_wait_gives_up_after_the_tolerated_failures(self) -> None:
        with Website() as server:
            server.truncate_responses = 5
            client = self.client(server)
            with self.assertRaises(ApiError):
                client.poll_run("run-1", interval=0.01, timeout=5)
            self.assertEqual(sum(item["method"] == "GET" for item in server.requests), 3)

    def test_incomplete_mutation_response_is_uncertain_and_never_retried(self) -> None:
        with Website() as server:
            client = self.client(server)
            server.truncate_responses = 1
            with self.assertRaises(ApiError) as caught:
                client.request("POST", "/runs/run-1/start")
            self.assertTrue(caught.exception.uncertain)
            self.assertTrue(caught.exception.transport)
            self.assertEqual(sum(item["method"] == "POST" for item in server.requests), 1)

    def test_saved_submission_id_survives_a_missing_archive(self) -> None:
        with Website() as server:
            server.download_failure = True
            result = self.upload(self.client(server))
            self.assertFalse(result["archive_verified"])
            self.assertEqual(result["submission"]["id"], "submission-1")
            self.assertEqual(result["archive_error"]["http_status"], 404)
            self.assertEqual(sum(item["method"] == "POST" for item in server.requests), 1)

    def test_downloaded_archive_must_match_uploaded_bytes(self) -> None:
        with Website() as server:
            server.mismatch = True
            self.assertFalse(self.upload(self.client(server))["archive_verified"])

    def test_create_failure_is_not_retried_or_started(self) -> None:
        with Website() as server:
            server.create_failure = True
            with self.assertRaises(ApiError) as caught:
                self.client(server).create_run("submission-1", "sample--a")
            self.assertEqual(caught.exception.status, 409)
            self.assertFalse(caught.exception.uncertain)
            self.assertEqual(
                [item["path"] for item in server.requests if item["method"] == "POST"],
                ["/api/runs"],
            )

    def test_start_failure_retains_created_id_without_creating_another_run(self) -> None:
        with Website() as server:
            server.start_failure = True
            code, output = self.run_cli(server, "run", "submission-1", "--task", "sample--a")
            self.assertEqual(code, 1)
            record = json.loads(output)
            self.assertEqual(record["run_id"], "run-1")
            self.assertTrue(record["start_error"]["outcome_uncertain"])
            self.assertEqual(
                [item["path"] for item in server.requests if item["method"] == "POST"],
                ["/api/runs", "/api/runs/run-1/start"],
            )

    def test_cancel_error_can_follow_a_state_change_without_retry(self) -> None:
        # An observed cancellation returned HTTP 500 after the state had changed.
        with Website() as server:
            code, output = self.run_cli(server, "cancel", "run-1")
            self.assertEqual(code, 1)
            record = json.loads(output)
            self.assertFalse(record["cancel_accepted"])
            self.assertEqual(record["run"]["status"], "CANCELLED")
            self.assertEqual(
                [item["path"] for item in server.requests if item["method"] == "POST"],
                ["/api/runs/run-1/cancel"],
            )

    def test_redirect_to_another_origin_is_refused(self) -> None:
        with Website() as server:
            client = self.client(server)
            server.redirect = "http://127.0.0.1:1/steal"
            with self.assertRaisesRegex(ApiError, "another origin"):
                client.request("GET", "/auth/me")

    def test_wait_polls_through_queued_without_starting_another_run(self) -> None:
        with Website() as server:
            server.run_states = ["QUEUED", "RUNNING", "PASSED"]
            code, output = self.run_cli(server, "wait", "run-1", "--interval", "0.001", "--timeout", "5")
            self.assertEqual(code, 0)
            self.assertEqual(
                [json.loads(line)["status"] for line in output.splitlines()],
                ["QUEUED", "RUNNING", "PASSED"],
            )
            self.assertFalse(any(item["method"] == "POST" for item in server.requests))

    def test_wait_reports_a_local_deadline_without_cancelling(self) -> None:
        with Website() as server:
            server.run_states = ["RUNNING"]
            code, output = self.run_cli(server, "wait", "run-1", "--interval", "0.01", "--timeout", "0.05")
            self.assertEqual(code, 2)
            last = json.loads(output.splitlines()[-1])
            self.assertTrue(last["waiting_timed_out"])
            self.assertFalse(last["run_cancelled"])
            self.assertFalse(any(item["method"] == "POST" for item in server.requests))

    # --- redaction and rendering ----------------------------------------

    def test_source_reads_one_file_and_saves_it_redacted_and_private(self) -> None:
        with Website() as server:
            destination = self.root / "trace.jsonl"
            code, output = self.run_cli(
                server, "source", "run-1", ".agent/trace.jsonl", "--output", str(destination)
            )
            self.assertEqual(code, 0)
            self.assertEqual(destination.read_text(encoding="utf-8"), "safe\n***")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertIn("file_path=.agent%2Ftrace.jsonl&kind=file", server.requests[-1]["path"])
            self.assertTrue(all(item["method"] == "GET" for item in server.requests))
            self.assertNotIn(MODEL_KEY, output)

    def test_logs_redact_the_account_key_in_free_text(self) -> None:
        with Website() as server:
            code, output = self.run_cli(server, "logs", "run-1", "--tail", "3")
            self.assertEqual(code, 0)
            record = json.loads(output)
            self.assertNotIn(MODEL_KEY, output)
            self.assertIn("***", record["console"])
            self.assertEqual(record["next_offset"], 42)

    def test_meter_uses_its_own_route_and_preserves_a_decimal_balance(self) -> None:
        # Route and field shapes were verified against the live meter; amounts are synthetic.
        with Website() as server:
            code, output = self.run_cli(server, "balance")
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(output),
                {
                    "available_balance": "0.000000",
                    "currency": "CNY",
                    "as_of": "2026-09-11T08:52:24Z",
                    "pending_billing_events": 0,
                },
            )
            self.assertEqual(
                [item["path"] for item in server.requests],
                ["/api/user/balance", "/api/user/freshness"],
            )

    def test_meter_identifier_is_not_printed_in_full(self) -> None:
        with Website() as server:
            code, output = self.run_cli(server, "whoami", "--meter")
            self.assertEqual(code, 0)
            self.assertNotIn("synthetic-meter-account-id", output)
            self.assertEqual(server.requests[-1]["path"], "/api/user/me")

    def test_status_full_response_is_redacted(self) -> None:
        with Website() as server:
            code, output = self.run_cli(server, "status", "run-1", "--full")
            self.assertEqual(code, 0)
            self.assertNotIn(SESSION_VALUE, output)

    def test_unauthenticated_session_reports_without_submitting(self) -> None:
        with Website() as server:
            server.reject_auth = True
            code, _ = self.run_cli(server, "whoami")
            self.assertEqual(code, 2)
            self.assertFalse(any(item["method"] == "POST" for item in server.requests))


class SummaryTests(unittest.TestCase):
    def test_real_status_fields_preserve_explicit_currency_and_test_counts(self) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/run-status-shape.json").read_text(encoding="utf-8")
        )
        summary = summarize_run(fixture["run"])
        self.assertEqual(summary["passed"], 2)
        self.assertEqual(summary["pass_rate"], 100.0)
        # The API names this field *_usd while returning an explicit CNY currency.
        self.assertEqual(summary["cost"], {"amount": 0.026695, "currency": "CNY"})
        self.assertEqual(summary["failed_tests"], [])
        self.assertEqual(len(summary["steps"]), 3)

    def test_log_tail_preserves_the_cursor_without_duplicate_streams(self) -> None:
        result = log_summary(
            "run-1",
            {
                "console": "1\n2\n3",
                "stdout": "1\n2\n3",
                "stderr": "error",
                "runner_events": ["large"],
                "log_offset": 42,
                "last_event_id": "event-1",
            },
            2,
        )
        self.assertEqual(result["console"], "2\n3")
        self.assertEqual(result["next_offset"], 42)
        self.assertNotIn("runner_events", result)
        self.assertNotIn("stdout", result)


if __name__ == "__main__":
    unittest.main()
