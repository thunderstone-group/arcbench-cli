"""Tests for the one part of the CLI that reads the browser."""

from __future__ import annotations

import platform
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arcbench_cli import session as session_module
from arcbench_cli.session import (
    cookie_plaintext,
    format_cookie_header,
    read_chrome_cookies,
    write_env_cookie,
)


class CookieLayoutTests(unittest.TestCase):
    def test_modern_layout_drops_the_domain_bound_hash(self) -> None:
        # Chrome 152 prefixes the plaintext with 32 effectively random bytes.
        digest = bytes(range(32))
        self.assertEqual(cookie_plaintext(digest + b"session-value"), "session-value")

    def test_legacy_layout_keeps_a_printable_plaintext(self) -> None:
        legacy = b"a" * 48
        self.assertEqual(cookie_plaintext(legacy), "a" * 48)

    def test_undecodable_plaintext_is_reported_not_guessed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not valid UTF-8"):
            cookie_plaintext(bytes([0x00] * 32) + b"\xff\xfe")


class EnvWritingTests(unittest.TestCase):
    def test_header_pairs_session_and_affinity_cookies(self) -> None:
        header = format_cookie_header(
            {"arcbench_session": "abc", "arc_api_sticky": "route-1"}
        )
        self.assertEqual(header, "arcbench_session=abc; arc_api_sticky=route-1")

    def test_write_replaces_one_line_and_keeps_the_file_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "ARC_BENCH_API_KEY=keep-me\nARC_BENCH_SESSION_COOKIE=stale\n", encoding="utf-8"
            )
            written = write_env_cookie(env_path, "arcbench_session=fresh")
            lines = written.read_text(encoding="utf-8").splitlines()
            self.assertIn("ARC_BENCH_API_KEY=keep-me", lines)
            self.assertIn("ARC_BENCH_SESSION_COOKIE=arcbench_session=fresh", lines)
            self.assertEqual(sum(line.startswith("ARC_BENCH_SESSION_COOKIE=") for line in lines), 1)
            self.assertEqual(stat.S_IMODE(written.stat().st_mode), 0o600)

    def test_write_creates_a_missing_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "nested" / ".env"
            written = write_env_cookie(env_path, "arcbench_session=fresh")
            self.assertTrue(written.is_file())


class PlatformGuardTests(unittest.TestCase):
    def test_non_macos_is_told_to_set_the_variable_by_hand(self) -> None:
        with patch.object(platform, "system", return_value="Linux"):
            with self.assertRaisesRegex(RuntimeError, "macOS Chrome only"):
                read_chrome_cookies()

    def test_missing_cookie_store_names_the_path(self) -> None:
        with patch.object(platform, "system", return_value="Darwin"):
            with self.assertRaisesRegex(RuntimeError, "cookie store not found"):
                read_chrome_cookies(chrome_root=Path("/nonexistent-chrome-root"))

    def test_keychain_refusal_is_actionable(self) -> None:
        class Refused:
            returncode = 1
            stdout = ""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Default"
            root.mkdir(parents=True)
            (root / "Cookies").write_bytes(b"")
            with (
                patch.object(platform, "system", return_value="Darwin"),
                patch.object(session_module.subprocess, "run", return_value=Refused()),
            ):
                with self.assertRaisesRegex(RuntimeError, "keychain prompt"):
                    read_chrome_cookies(chrome_root=Path(tmp))


if __name__ == "__main__":
    unittest.main()
