from __future__ import annotations

import unittest

from arcbench_cli.cli import build_parser, extract_marked_block


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


if __name__ == "__main__":
    unittest.main()
