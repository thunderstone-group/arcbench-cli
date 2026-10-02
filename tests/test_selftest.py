"""Self-test integration contract, using a sanitized real organizer response."""

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from arcbench_cli import cli, session
from arcbench_cli.client import ApiError, SubmitConfig
from arcbench_cli.selftest import SelftestClient, result_exit, validate_app

FIXTURE = json.loads((Path(__file__).parent / "fixtures/selftest-result.json").read_text())


class SelftestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.zip = self.root / "app.zip"
        with zipfile.ZipFile(self.zip, "w") as archive:
            archive.writestr("Dockerfile", "FROM node:22-alpine\n")
            archive.writestr("src/app.js", "// application")
        self.client = SelftestClient.from_env({
            "ARC_BENCH_SESSION_COOKIE": "arcbench_session=competition-secret",
            "ARC_BENCH_API_KEY": "model-secret",
            "ARC_BENCH_SELFTEST_COOKIE": "__Secure-next-auth.session-token=selftest-secret",
        })

    def test_separate_service_never_receives_competition_credentials(self):
        self.assertEqual(self.client.config.api_key, "")
        cookies = {c.name: c.value for c in self.client.cookies}
        self.assertEqual(cookies, {"__Secure-next-auth.session-token": "selftest-secret"})
        self.assertEqual(self.client.safe("selftest-secret"), "***")
        self.assertIn("arcbench-selftest-web.vercel.app", self.client.config.base_url)

    def test_real_failed_response_preserves_diagnostics(self):
        with patch.object(self.client, "request", return_value=FIXTURE) as request:
            result = self.client.submission("fixture-submission")
        request.assert_called_once_with("GET", "/submissions/fixture-submission")
        self.assertEqual(result["result"]["passed"], 10)
        # Only three diagnostic entries are retained in the public fixture.
        # Preserve the upstream aggregate rather than recomputing it from this sample.
        self.assertEqual(result["result"]["total"], 30)
        self.assertEqual(len(result["result"]["tests"]), 3)
        self.assertEqual(sum(not t["ok"] for t in result["result"]["tests"]), 1)
        self.assertEqual(result_exit(result), 1)
        self.assertIn("strict mode violation", result["result"]["tests"][2]["error"])

    def test_status_cannot_be_misattributed(self):
        with patch.object(self.client, "request", return_value=FIXTURE):
            with self.assertRaisesRegex(ApiError, "different submission"):
                self.client.submission("different-submission")

    def test_wait_timeout_does_not_create_a_run_or_quality_failure(self):
        pending = {"id": "fixture-submission", "status": "queued"}
        with patch.object(self.client, "submission", return_value=pending) as status:
            result = self.client.wait_submission("fixture-submission", timeout=0)
        self.assertEqual(result["status"], "queued")
        self.assertTrue(result["poll_timed_out"])
        self.assertEqual(result_exit(result), 2)
        status.assert_called_once()

    def test_wait_reads_same_submission_until_real_terminal(self):
        with patch.object(self.client, "submission", side_effect=[
            {"id": "fixture-submission", "status": "running"}, FIXTURE["submission"]
        ]) as status, patch("arcbench_cli.selftest.time.sleep"):
            result = self.client.wait_submission("fixture-submission")
        self.assertEqual(result["result"]["total"], 30)
        self.assertEqual([call.args for call in status.call_args_list], [("fixture-submission",)] * 2)

    def test_read_tls_failure_is_retried_without_another_upload(self):
        # Observed on 2026-10-02 after the service accepted an upload: GET status
        # returned URLError/SSLEOFError with transport_failure=true, not a verdict.
        failure = ApiError("TLS unexpected EOF", transport=True, method="GET", kind="tls")
        with patch.object(self.client, "submission", side_effect=[failure, FIXTURE["submission"]]) as status, \
                patch.object(self.client, "upload_application") as upload, \
                patch("arcbench_cli.selftest.time.sleep"):
            result = self.client.wait_submission("fixture-submission")
        self.assertEqual(result["result"]["passed"], 10)
        self.assertEqual(status.call_count, 2)
        upload.assert_not_called()

    def test_repeated_read_failure_is_bounded_and_auth_failure_is_not_retried(self):
        for error, expected_calls in ((ApiError("TLS EOF", transport=True), 3),
                                       (ApiError("sign in required", status=401), 1)):
            with self.subTest(error=str(error)), patch.object(self.client, "submission", side_effect=error) as status, \
                    patch("arcbench_cli.selftest.time.sleep"):
                with self.assertRaises(ApiError):
                    self.client.wait_submission("fixture-submission")
            self.assertEqual(status.call_count, expected_calls)

    def test_read_failure_at_deadline_remains_a_local_timeout(self):
        with patch.object(self.client, "submission", side_effect=ApiError("TLS EOF", transport=True)):
            result = self.client.wait_submission("fixture-submission", timeout=0)
        self.assertTrue(result["poll_timed_out"])
        self.assertIsNone(result["status"])
        self.assertEqual(result_exit(result), 2)

    def test_upload_follows_live_web_protocol_without_forwarding_cookies(self):
        signed = "https://storage.example.test/app.zip?signature=private-signature"
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        storage = Mock()
        storage.open.return_value = response
        with patch.object(self.client, "_post", side_effect=[
            {"id": "upload-1", "uploadUrl": signed}, {"id": "new-submission"}
        ]) as post, patch("arcbench_cli.selftest.urllib.request.build_opener", return_value=storage):
            result = self.client.upload_application("github-stage-1-req-test", self.zip)
        self.assertEqual(post.call_args_list[0].args, (
            "/upload-url", {"taskId": "github-stage-1-req-test", "size": self.zip.stat().st_size}))
        self.assertEqual(post.call_args_list[1].args, ("/submit", {"uploadId": "upload-1"}))
        req = storage.open.call_args.args[0]
        self.assertEqual(req.get_method(), "PUT")
        self.assertEqual(req.data, self.zip.read_bytes())
        self.assertNotIn("Cookie", req.headers)
        self.assertNotIn("Authorization", req.headers)
        self.assertFalse(result["counts_on_leaderboard"])
        self.assertEqual(result["id"], "new-submission")
        self.assertNotIn(signed, json.dumps(result))
        self.assertEqual(self.client.safe(signed), "***")

    def test_failed_object_upload_does_not_submit_or_retry(self):
        storage = Mock()
        storage.open.side_effect = urllib.error.URLError("disconnected")
        with patch.object(self.client, "_post", return_value={
            "id": "upload-1", "uploadUrl": "https://storage.example.test/app.zip?signature=secret"
        }) as post, patch("arcbench_cli.selftest.urllib.request.build_opener", return_value=storage):
            with self.assertRaises(ApiError) as failure:
                self.client.upload_application("github-stage-1-req-test", self.zip)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(storage.open.call_count, 1)
        self.assertTrue(failure.exception.uncertain)
        self.assertEqual(failure.exception.details["upload_id"], "upload-1")
        self.assertNotIn("signature", str(failure.exception))

    def test_zip_rejected_before_allocating_quota(self):
        for bad in ("nested/Dockerfile", "../Dockerfile", "C:\\Dockerfile", "node_modules/file"):
            with self.subTest(bad=bad):
                with zipfile.ZipFile(self.zip, "w") as archive:
                    if bad != "nested/Dockerfile":
                        archive.writestr("Dockerfile", "FROM node:22-alpine")
                    archive.writestr(bad, "invalid")
                with patch.object(self.client, "_post") as post:
                    with self.assertRaises(ValueError):
                        self.client.upload_application("github-stage-1-req-test", self.zip)
                post.assert_not_called()

    def test_cli_json_status_uses_real_response_and_saves_result(self):
        out = self.root / "result.json"
        stdout = io.StringIO()
        with patch.object(cli.SelftestClient, "from_env", return_value=self.client), \
                patch.object(self.client, "request", return_value=FIXTURE), contextlib.redirect_stdout(stdout):
            code = cli.main(["selftest", "status", "fixture-submission", "--json", "--output", str(out)])
        self.assertEqual(code, 1)
        record = json.loads(stdout.getvalue())
        self.assertEqual(record["result"]["passed"], 10)
        self.assertFalse(record["counts_on_leaderboard"])
        self.assertEqual(json.loads(out.read_text()), record)

    def test_parser_flags_work_at_all_supported_levels(self):
        for argv in (["--json", "selftest", "tasks"], ["selftest", "--json", "tasks"],
                     ["selftest", "tasks", "--json"]):
            self.assertTrue(cli.build_parser().parse_args(argv).json)
        args = cli.build_parser().parse_args([
            "selftest", "submit", "app.zip", "--task", "github-stage-1-req-test", "--wait"])
        self.assertEqual(args.func, cli.cmd_selftest)

    def test_selftest_cookie_capture_is_exact_domain_and_session_only(self):
        root = self.root / "Default"
        root.mkdir()
        db = sqlite3.connect(root / "Cookies")
        db.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
        rows = [
            ("arcbench-selftest-web.vercel.app", "__Secure-next-auth.session-token.0", b"v10one"),
            ("arcbench-selftest-web.vercel.app", "__Secure-next-auth.session-token.1", b"v10two"),
            ("arcbench-selftest-web.vercel.app.attacker.test", "__Secure-next-auth.session-token", b"v10bad"),
            ("arcbench-selftest-web.vercel.app", "__Host-next-auth.csrf-token", b"v10csrf"),
            ("github.com", "__Secure-next-auth.session-token", b"v10github"),
        ]
        db.executemany("INSERT INTO cookies VALUES (?, ?, ?)", rows)
        db.commit()
        db.close()
        with patch.object(session.platform, "system", return_value="Darwin"), \
                patch.object(session, "_safe_storage_key", return_value=b"unused"), \
                patch.object(session, "decrypt_cbc", side_effect=lambda data, *_: data), \
                patch.object(session, "strip_pkcs7", side_effect=lambda data: data):
            cookies = session.read_chrome_cookies(chrome_root=self.root, selftest=True)
        self.assertEqual(cookies, {"__Secure-next-auth.session-token.0": "one",
                                   "__Secure-next-auth.session-token.1": "two"})


if __name__ == "__main__":
    unittest.main()
