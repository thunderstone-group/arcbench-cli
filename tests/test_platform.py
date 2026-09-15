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
    aggregate_usage,
    log_summary,
    summarize_request,
    summarize_run,
    usage_total,
)
from arcbench_cli.cli import main

SESSION_VALUE = "synthetic-session-secret"
MODEL_KEY = "synthetic-model-secret"

# Meter payloads are the live service's own responses with the account
# identifiers removed; see the file's own `_source` note.
METER = json.loads(
    (Path(__file__).parent / "fixtures/meter-responses.json").read_text(encoding="utf-8")
)


class Website:
    """A minimal stand-in for the platform's HTTP surface."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.archive: bytes | None = None
        self.download_failure = False
        self.create_failure = False
        self.start_failure = False
        self.start_failures: set[str] = set()
        self.reject_auth = False
        self.mismatch = False
        self.redirect: str | None = None
        self.run_states = ["PASSED"]
        self.states: dict[str, list[str]] = {}
        self.created_runs = 0
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
                extra_headers: list[tuple[str, str]] = []
                path = self.path
                if path == "/api/auth/me":
                    status, result = (
                        (401, {"detail": "not authenticated"})
                        if owner.reject_auth
                        else (200, {"user": {"username": "synthetic-user"}})
                    )
                elif path == "/api/user/login":
                    if json.loads(raw or b"{}").get("access_key") == MODEL_KEY:
                        result = METER["login"]
                        extra_headers.append(("Set-Cookie", METER["set_cookie"]))
                    else:
                        status, result = 401, METER["login_rejected"]
                elif path == "/api/user/me":
                    result = METER["me"]
                elif path == "/api/user/balance":
                    result = METER["balance"]
                elif path == "/api/user/freshness":
                    result = METER["freshness"]
                elif path == "/api/user/models":
                    result = METER["models"]
                elif path.split("?")[0] == "/api/user/usage":
                    # The live service ignores granularity and answers hourly.
                    result = METER["usage"]
                elif path.split("?")[0] == "/api/user/requests":
                    # The live service ignores limit and returns everything,
                    # oldest first.
                    result = METER["requests"]
                elif path == "/api/competitions/sample":
                    result = {
                        "id": "sample",
                        "title": "Sample competition",
                        "tasks": [{"id": "sample--a"}, {"id": "sample--b"}],
                    }
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
                    owner.created_runs += 1
                    status, result = (
                        (409, {"detail": "The submitted agent archive is no longer available"})
                        if owner.create_failure
                        else (
                            200,
                            {"run": {"id": f"run-{owner.created_runs}", "status": "PENDING"}},
                        )
                    )
                elif path.startswith("/api/runs/"):
                    run_id, _, action = path[len("/api/runs/") :].partition("/")
                    action = action.split("?")[0]
                    states = owner.states.get(run_id, owner.run_states)
                    if action == "start":
                        failed = owner.start_failure or run_id in owner.start_failures
                        status, result = (
                            (503, {"detail": "upstream unavailable"})
                            if failed
                            else (200, {"id": run_id, "status": "RUNNING"})
                        )
                    elif action == "cancel":
                        owner.run_states = ["CANCELLED"]
                        status, result = 500, {"detail": "Internal Server Error"}
                    elif action == "logs":
                        result = {"console": f"line-1\nkey {MODEL_KEY}\nline-3", "log_offset": 42}
                    elif action == "source":
                        result = {
                            "file_path": ".agent/trace.jsonl",
                            "kind": "file",
                            "content": f"safe\n{MODEL_KEY}",
                        }
                    else:
                        result = {"id": run_id, "status": states[0]}
                        if len(states) > 1:
                            states.pop(0)
                body = result if isinstance(result, bytes) else json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", mime)
                for name, value in extra_headers:
                    self.send_header(name, value)
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

    def run_cli(self, server: Website, *argv: str, env_lines: list[str] | None = None) -> tuple[int, str]:
        if env_lines is None:
            env_lines = [
                f"ARC_BENCH_SESSION_COOKIE=arcbench_session={SESSION_VALUE}",
                f"ARC_BENCH_METER_COOKIE=arcbench_session={SESSION_VALUE}",
            ]
        self.env_file.write_text(
            "\n".join([*env_lines, f"ARC_BENCH_METER_BASE_URL={server.origin}"]) + "\n",
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
            record = json.loads(output)[0]
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

    # --- concurrency -----------------------------------------------------

    def test_run_starts_one_run_per_task_and_reports_each(self) -> None:
        with Website() as server:
            code, output = self.run_cli(
                server, "run", "submission-1", "--task", "sample--a", "--task", "sample--b"
            )
            self.assertEqual(code, 0)
            records = json.loads(output)
            self.assertEqual(
                [(item["task"], item["run_id"], item["run"]["status"]) for item in records],
                [("sample--a", "run-1", "RUNNING"), ("sample--b", "run-2", "RUNNING")],
            )
            self.assertEqual(
                [item["path"] for item in server.requests if item["method"] == "POST"],
                ["/api/runs", "/api/runs/run-1/start", "/api/runs", "/api/runs/run-2/start"],
            )

    def test_one_failed_start_does_not_stop_the_remaining_tasks(self) -> None:
        with Website() as server:
            server.start_failures = {"run-1"}
            code, output = self.run_cli(
                server, "run", "submission-1", "--task", "sample--a", "--task", "sample--b"
            )
            self.assertEqual(code, 1)
            failed, started = json.loads(output)
            self.assertEqual(failed["run_id"], "run-1")
            self.assertTrue(failed["start_error"]["outcome_uncertain"])
            self.assertIn("arcbench start run-1", failed["next"])
            self.assertEqual(started["run"]["status"], "RUNNING")

    def test_all_tasks_resolves_the_competition_from_the_submission(self) -> None:
        with Website() as server:
            code, output = self.run_cli(server, "run", "submission-1", "--all-tasks")
            self.assertEqual(code, 0)
            self.assertEqual(
                [item["task"] for item in json.loads(output)], ["sample--a", "sample--b"]
            )
            gets = [item["path"] for item in server.requests if item["method"] == "GET"]
            self.assertEqual(gets[:2], ["/api/submissions", "/api/competitions/sample"])

    def test_all_tasks_on_an_unknown_submission_starts_nothing(self) -> None:
        with Website() as server:
            code, _ = self.run_cli(server, "run", "submission-404", "--all-tasks")
            self.assertEqual(code, 1)
            self.assertFalse(any(item["method"] == "POST" for item in server.requests))

    def test_run_without_a_task_selection_starts_nothing(self) -> None:
        with Website() as server:
            self.assertEqual(self.run_cli(server, "run", "submission-1")[0], 1)
            self.assertFalse(any(item["method"] == "POST" for item in server.requests))

    def test_wait_polls_several_runs_round_robin_until_all_finish(self) -> None:
        with Website() as server:
            server.states = {"run-1": ["RUNNING", "PASSED"], "run-2": ["QUEUED", "RUNNING", "PASSED"]}
            code, output = self.run_cli(
                server, "wait", "run-1", "run-2", "--interval", "0.001", "--timeout", "5"
            )
            self.assertEqual(code, 0)
            rows = [json.loads(line) for line in output.splitlines()]
            self.assertEqual(
                [(row["run_id"], row["status"]) for row in rows],
                [
                    ("run-1", "RUNNING"),
                    ("run-2", "QUEUED"),
                    ("run-1", "PASSED"),
                    ("run-2", "RUNNING"),
                    ("run-2", "PASSED"),
                ],
            )
            self.assertFalse(any(item["method"] == "POST" for item in server.requests))

    def test_wait_on_several_runs_reports_a_non_passing_one(self) -> None:
        with Website() as server:
            server.states = {"run-1": ["PASSED"], "run-2": ["FAILED"]}
            code, _ = self.run_cli(
                server, "wait", "run-1", "run-2", "--interval", "0.001", "--timeout", "5"
            )
            self.assertEqual(code, 1)

    def test_wait_on_several_runs_reports_the_deadline_per_run(self) -> None:
        with Website() as server:
            server.states = {"run-1": ["PASSED"], "run-2": ["RUNNING"]}
            code, output = self.run_cli(
                server, "wait", "run-1", "run-2", "--interval", "0.01", "--timeout", "0.05"
            )
            self.assertEqual(code, 2)
            last = json.loads(output.splitlines()[-1])
            self.assertEqual(last["run_id"], "run-2")
            self.assertTrue(last["waiting_timed_out"])
            self.assertFalse(last["run_cancelled"])

    def test_status_reads_several_runs_one_record_each(self) -> None:
        with Website() as server:
            server.states = {"run-1": ["PASSED"], "run-2": ["FAILED"]}
            code, output = self.run_cli(server, "status", "run-1", "run-2")
            self.assertEqual(code, 1)
            self.assertEqual(
                [json.loads(line)["status"] for line in output.splitlines()],
                ["PASSED", "FAILED"],
            )

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

    def test_meter_cookie_still_overrides_the_key_login(self) -> None:
        # Routes and field shapes come from the live meter; see the fixture.
        with Website() as server:
            code, output = self.run_cli(server, "balance")
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(output),
                {
                    "account": "acco....com",
                    "available_balance": "-1.353979",
                    "currency": "CNY",
                    "as_of": "2026-09-15T03:05:11Z",
                    "pending_billing_events": 0,
                },
            )
            # A configured cookie means no login request is made at all.
            self.assertEqual(
                [item["path"] for item in server.requests],
                ["/api/user/balance", "/api/user/freshness"],
            )

    def test_meter_logs_in_with_the_access_key_and_holds_the_cookie(self) -> None:
        with Website() as server:
            code, output = self.run_cli(
                server, "balance", env_lines=[f"ARC_BENCH_API_KEY={MODEL_KEY}"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["available_balance"], "-1.353979")
            self.assertEqual(
                [item["path"] for item in server.requests],
                ["/api/user/login", "/api/user/balance", "/api/user/freshness"],
            )
            login, balance = server.requests[0], server.requests[1]
            self.assertEqual(login["method"], "POST")
            self.assertIsNone(login["cookie"])
            self.assertIn("onr_user_session=redacted-session", balance["cookie"])
            self.assertNotIn(MODEL_KEY, output)

    def test_meter_falls_back_to_the_account_key_when_none_is_configured(self) -> None:
        with Website() as server:
            code, _ = self.run_cli(
                server,
                "balance",
                env_lines=[f"ARC_BENCH_SESSION_COOKIE=arcbench_session={SESSION_VALUE}"],
            )
            self.assertEqual(code, 0)
            self.assertEqual(
                [item["path"] for item in server.requests][:2],
                ["/api/auth/access-key", "/api/user/login"],
            )

    def test_meter_login_failure_reports_without_echoing_the_key(self) -> None:
        with Website() as server:
            code, output = self.run_cli(
                server, "balance", env_lines=["ARC_BENCH_API_KEY=wrong-key-value"]
            )
            self.assertEqual(code, 1)
            self.assertNotIn("wrong-key-value", output)
            self.assertEqual([item["path"] for item in server.requests], ["/api/user/login"])

    def test_models_prints_the_gateway_price_table(self) -> None:
        with Website() as server:
            code, output = self.run_cli(
                server, "models", env_lines=[f"ARC_BENCH_API_KEY={MODEL_KEY}"]
            )
            self.assertEqual(code, 0)
            models = json.loads(output)
            self.assertEqual([model["id"] for model in models][:2],
                             ["deepseek-v4-flash", "deepseek-v4-pro"])
            self.assertEqual(models[0]["pricing"]["unit"], "CNY / 1M tokens")
            self.assertEqual(server.requests[-1]["path"], "/api/user/models")

    def test_usage_keeps_the_server_row_shape_and_totals_in_decimal(self) -> None:
        with Website() as server:
            code, output = self.run_cli(
                server, "usage", env_lines=[f"ARC_BENCH_API_KEY={MODEL_KEY}"]
            )
            self.assertEqual(code, 0)
            rows = json.loads(output)
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]["dimensions"]["bucket"], "2026-09-14T03:00:00+00:00")
            self.assertEqual(rows[0]["measures"]["amount"], "1.404476")
            self.assertEqual(usage_total(rows)["amount"], "40.057251")
            self.assertEqual(server.requests[-1]["path"], "/api/user/usage?granularity=hour")

    def test_usage_day_merges_hour_buckets_the_service_will_not(self) -> None:
        rows = aggregate_usage(METER["usage"]["rows"], "day")
        self.assertEqual(
            [(row["dimensions"]["bucket"], row["dimensions"]["model"]) for row in rows],
            [
                ("2026-09-14", "deepseek-v4-flash"),
                ("2026-09-14", "kimi-k3"),
                ("2026-09-15", "deepseek-v4-flash"),
            ],
        )
        # 1.404476 + 3.202565, added as decimals rather than floats.
        self.assertEqual(rows[0]["measures"]["amount"], "4.607041")
        self.assertEqual(rows[0]["measures"]["prompt_tokens"], 125832 + 673118)
        self.assertEqual(rows[0]["measures"]["usage_event_count"], 78)

    def test_usage_since_and_model_select_client_side(self) -> None:
        rows = METER["usage"]["rows"]
        self.assertEqual(
            [row["dimensions"]["model"] for row in aggregate_usage(rows, "hour", model="kimi-k3")],
            ["kimi-k3"],
        )
        recent = aggregate_usage(rows, "hour", since="2026-09-15T00:00:00Z")
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["measures"]["amount"], "35.183570")
        self.assertEqual(aggregate_usage(rows, "hour", since="2030-01-01T00:00:00Z"), [])
        self.assertEqual(usage_total([])["amount"], "0")

    def test_requests_returns_the_newest_entries_first(self) -> None:
        with Website() as server:
            code, output = self.run_cli(
                server, "requests", "--limit", "2", env_lines=[f"ARC_BENCH_API_KEY={MODEL_KEY}"]
            )
            self.assertEqual(code, 0)
            entries = json.loads(output)
            self.assertEqual(
                [entry["request_id"] for entry in entries],
                ["2026091510583767756231956632", "2026091411342407200837985435"],
            )
            self.assertEqual(server.requests[-1]["path"], "/api/user/requests?limit=2")

    def test_request_summary_renders_epoch_seconds_as_utc(self) -> None:
        summary = summarize_request(METER["requests"]["requests"][0])
        self.assertEqual(summary["occurred_at"], "2026-09-14T03:16:53Z")
        self.assertEqual(summary["model"], "kimi-k3")
        self.assertEqual(summary["total_tokens"], 8721)
        self.assertEqual(summary["amount"], "0.284180")

    def test_meter_identifier_is_not_printed_in_full(self) -> None:
        with Website() as server:
            code, output = self.run_cli(server, "whoami", "--meter")
            self.assertEqual(code, 0)
            self.assertNotIn("account@example.com", output)
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
