"""Synthetic launch checks only: no sockets, credentials, or competition writes."""

from __future__ import annotations

import errno
import io
import os
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from arcbench_cli import cli, preflight
from arcbench_cli.client import ApiError, OfficialClient, RunStartError, SubmitConfig
from arcbench_cli.preflight import LaunchPreflight, PreflightError


def balance(**changes):
    return {
        "available_balance": "12.50", "currency": "CNY",
        "pending_billing_events": 0, "as_of": "2026-09-16T00:00:00Z",
        **changes,
    }


class SyntheticTestCase(unittest.TestCase):
    def setUp(self):
        # Accidental integration calls fail immediately, even outside the sandbox.
        for target in (
            "socket.socket", "arcbench_cli.client.OfficialClient._request",
            "arcbench_cli.cli.load_env", "arcbench_cli.cli.capture_chrome_cookie",
        ):
            guard = patch(target, side_effect=AssertionError(f"forbidden in synthetic tests: {target}"))
            guard.start()
            self.addCleanup(guard.stop)

    def assert_reason(self, reason, action):
        with self.assertRaises(PreflightError) as caught:
            action()
        self.assertEqual(caught.exception.details["phase"], "preflight")
        self.assertEqual(caught.exception.details["reason"], reason)
        return caught.exception


class LaunchPreflightTests(SyntheticTestCase):
    def setUp(self):
        super().setUp()
        self.website = Mock(spec=OfficialClient)
        self.website.list_runs.return_value = []
        self.meter = Mock(spec=OfficialClient)
        self.meter.balance.return_value = balance()
        self.factory = Mock(return_value=self.meter)

    def check(self, **options):
        return LaunchPreflight(self.website, self.factory, **options).check()

    def test_positive_balance_and_exact_minimum(self):
        result = self.check(minimum="12.50")
        self.assertEqual(result["available_balance"], "12.50")
        self.assertEqual(result["min_balance"], "12.50")
        self.assertEqual(result["active_run_ids"], [])
        self.website.list_runs.assert_called_once_with(limit=None)

    def test_zero_negative_and_below_minimum_balances(self):
        for amount, minimum in (("0", "0"), ("-1.35", "0"), ("1.249", "1.25")):
            with self.subTest(amount=amount, minimum=minimum):
                self.meter.balance.return_value = balance(available_balance=amount)
                self.assert_reason("insufficient_balance", lambda: self.check(minimum=minimum))
        self.website.list_runs.assert_not_called()

    def test_unknown_balance_or_currency(self):
        snapshots = [None, [], {}, *[
            balance(available_balance=value) for value in (None, "", "bad", "NaN", "Infinity", True)
        ], *[balance(currency=value) for value in (None, "", " ", 1)]]
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                self.meter.balance.return_value = snapshot
                self.assert_reason("balance_unknown", self.check)

    def test_invalid_minimum(self):
        for minimum in ("-1", "NaN", "Infinity", "bad", "", None):
            with self.subTest(minimum=minimum):
                self.assert_reason("invalid_min_balance", lambda: self.check(minimum=minimum))
        self.factory.assert_not_called()

    def test_meter_unavailable(self):
        for error in (ApiError("offline"), RuntimeError("no key"), ValueError("bad"), OSError("offline")):
            with self.subTest(error=error):
                self.factory.side_effect = error
                self.assert_reason("balance_unavailable", self.check)
        self.factory.side_effect = None
        self.meter.balance.side_effect = ApiError("offline")
        self.assert_reason("balance_unavailable", self.check)

    def test_pending_billing(self):
        self.meter.balance.return_value = balance(pending_billing_events=1)
        self.assert_reason("pending_billing", self.check)

    def test_unknown_pending_billing(self):
        for pending in (None, "0", 0.0, True, -1):
            with self.subTest(pending=pending):
                self.meter.balance.return_value = balance(pending_billing_events=pending)
                self.assert_reason("meter_unknown", self.check)

    def test_invalid_timestamp(self):
        for stamp in (None, "", "not-a-time", "2026-02-30T00:00:00Z", 123):
            with self.subTest(stamp=stamp):
                self.meter.balance.return_value = balance(as_of=stamp)
                self.assert_reason("meter_unknown", self.check)

    def test_pending_queued_and_running_block_default_launch(self):
        for status in ("PENDING", "QUEUED", "RUNNING"):
            with self.subTest(status=status):
                self.website.list_runs.return_value = [{"id": "other", "status": status}]
                error = self.assert_reason("active_runs", self.check)
                self.assertEqual(error.details["run_ids"], ["other"])

    def test_only_target_pending_run_is_exempt(self):
        self.website.list_runs.return_value = [{"id": "target", "status": "PENDING"}]
        check = LaunchPreflight(self.website, self.factory)
        self.assertEqual(check.check(target_run="target")["active_run_ids"], [])
        for status in ("QUEUED", "RUNNING"):
            self.website.list_runs.return_value = [{"id": "target", "status": status}]
            self.assert_reason("active_runs", lambda: check.check(target_run="target"))

    def test_terminal_runs_do_not_block(self):
        self.website.list_runs.return_value = [
            {"id": status, "status": status} for status in
            ("PASSED", "FAILED", "ERROR", "CANCELLED", "CANCELED", "TIMEOUT", "PAUSED")
        ]
        self.assertEqual(self.check()["active_run_ids"], [])

    def test_run_list_unavailable_or_unknown(self):
        self.website.list_runs.side_effect = ApiError("offline")
        self.assert_reason("runs_unavailable", self.check)
        self.website.list_runs.side_effect = None
        for runs in ({}, None, [None], [{}], [{"id": "r", "status": "new-state"}]):
            with self.subTest(runs=runs):
                self.website.list_runs.return_value = runs
                self.assert_reason("runs_unknown", self.check)

    def test_multiple_tasks_require_explicit_concurrency(self):
        check = LaunchPreflight(self.website, self.factory)
        self.assert_reason("concurrent_tasks", lambda: check.check(task_count=2))
        self.factory.assert_not_called()

    def test_explicit_concurrency_accepts_overlap(self):
        self.website.list_runs.return_value = [{"id": "other", "status": "RUNNING"}]
        result = LaunchPreflight(self.website, self.factory, allow_concurrent=True).check(task_count=2)
        self.assertTrue(result["allow_concurrent"])
        self.assertEqual(result["active_run_ids"], ["other"])

    def test_explicit_concurrency_does_not_bypass_meter_checks(self):
        for snapshot, reason in (
            (balance(available_balance="0"), "insufficient_balance"),
            (balance(available_balance=None), "balance_unknown"),
            (balance(pending_billing_events=1), "pending_billing"),
            (balance(as_of="bad"), "meter_unknown"),
        ):
            with self.subTest(reason=reason):
                self.meter.balance.return_value = snapshot
                self.assert_reason(reason, lambda: self.check(allow_concurrent=True))

    def test_each_check_refreshes_balance_and_runs_but_reuses_meter(self):
        check = LaunchPreflight(self.website, self.factory)
        check.check()
        check.check()
        self.factory.assert_called_once_with()
        self.assertEqual(self.meter.balance.call_count, 2)
        self.assertEqual(self.website.list_runs.call_count, 2)
        self.meter.balance.return_value = balance(available_balance="0")
        self.assert_reason("insufficient_balance", check.check)


@contextmanager
def mocked_lock(*, busy=False):
    """Exercise the real POSIX context manager with every OS operation mocked."""
    lock_path = Mock(spec=Path)
    state = {"held": False}

    def acquire(*_):
        if busy:
            raise BlockingIOError(errno.EAGAIN, "busy")
        state["held"] = True

    def close(_):
        state["held"] = False

    with (
        patch.object(preflight, "launch_lock_path", return_value=lock_path),
        patch.object(preflight.os, "open", return_value=73) as opened,
        patch.object(preflight.os, "close", side_effect=close) as closed,
        patch("fcntl.flock", side_effect=acquire) as flock,
    ):
        yield state, opened, closed, flock


@unittest.skipIf(os.name == "nt", "POSIX lock branch; Windows requires separate validation")
class LaunchCommandTests(SyntheticTestCase):
    @contextmanager
    def command(self, name, extra=()):
        config = SubmitConfig(session_cookie="synthetic-session", api_key="synthetic-key")
        website = Mock(spec=OfficialClient)
        website.config = config
        website.known_secrets = []
        website.check_login.return_value = {"logged_in": True}
        website.list_tasks.return_value = [{"id": "c--a"}, {"id": "c--b"}]
        website.tasks_for_submission.return_value = ["c--a", "c--b"]
        website.list_runs.return_value = []
        website.get_run.return_value = {"id": "r", "status": "PENDING"}
        website.create_run.return_value = {"id": "r", "status": "PENDING"}
        website.upload.return_value = {"submission": {"id": "s"}, "archive_verified": True}
        website.start_run_with_queue_wait.side_effect = lambda *a, **kw: OfficialClient.start_run_with_queue_wait(website, *a, **kw)
        website.start_run.return_value = {"id": "r", "status": "RUNNING"}
        meter = Mock(spec=OfficialClient)
        meter.balance.return_value = balance()
        argv = {
            "run": ["run", "s", "--task", "c--a"],
            "start": ["start", "r"],
            "submit": ["submit", "--package", "synthetic.zip", "--competition", "c",
                       "--task", "c--a", "--record-dir", "/tmp/synthetic-records", "--no-wait"],
        }[name] + list(extra)
        args = cli.build_parser().parse_args(argv)
        with (
            patch.object(cli, "_client", return_value=({}, website)) as client_factory,
            patch.object(cli, "_config", return_value=({}, config)) as config_factory,
            patch.object(cli, "OfficialClient", return_value=website) as constructor,
            patch.object(cli, "_meter_from_config", return_value=meter),
            patch.object(cli, "validate_package", return_value={"sha256": "0" * 64, "bytes": 1}),
            patch.object(cli, "_resolve_key", return_value=None),
            patch.object(cli, "enforce_budget"), patch.object(cli, "write_record"),
            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()),
        ):
            yield args, website, meter, client_factory, config_factory, constructor

    def assert_no_writes(self, website):
        for name in ("upload", "create_run", "start_run_with_queue_wait", "start_run"):
            getattr(website, name).assert_not_called()

    def test_rejected_commands_have_no_upload_create_or_start(self):
        for name in ("run", "start", "submit"):
            for reason, snapshot, runs, extra in (
                ("insufficient_balance", balance(available_balance="0"), [], []),
                ("insufficient_balance", balance(available_balance="-1"), [], []),
                ("balance_unknown", balance(available_balance=None), [], []),
                ("pending_billing", balance(pending_billing_events=1), [], []),
                ("meter_unknown", balance(as_of="bad"), [], []),
                ("active_runs", balance(), [{"id": "other", "status": "RUNNING"}], []),
                ("insufficient_balance", balance(), [], ["--min-balance", "13"]),
                ("pending_billing", balance(pending_billing_events=1), [], ["--allow-concurrent"]),
            ):
                with self.subTest(command=name, reason=reason, extra=extra), mocked_lock(), self.command(name, extra) as setup:
                    args, website, meter, *_ = setup
                    meter.balance.return_value = snapshot
                    website.list_runs.return_value = runs
                    self.assert_reason(reason, lambda: args.func(args))
                    self.assert_no_writes(website)

    def test_multi_task_rejection_precedes_all_writes(self):
        for name in ("run", "submit"):
            for extra in (["--task", "c--b"], ["--all-tasks"]):
                with self.subTest(command=name, extra=extra), mocked_lock(), self.command(name, extra) as setup:
                    args, website, *_ = setup
                    self.assert_reason("concurrent_tasks", lambda: args.func(args))
                    self.assert_no_writes(website)

    def test_start_requires_confirmed_pending_state(self):
        for status in (None, "RUNNING", "PASSED"):
            with self.subTest(status=status), mocked_lock(), self.command("start") as setup:
                args, website, *_ = setup
                website.get_run.return_value = {"id": "r", "status": status}
                self.assert_reason("run_not_pending", lambda: args.func(args))
                self.assert_no_writes(website)

    def test_lock_held_before_client_and_config_until_command_returns(self):
        for name in ("run", "start", "submit"):
            with self.subTest(command=name), mocked_lock() as lock, self.command(name) as setup:
                state, opened, closed, flock = lock
                args, website, _, client_factory, config_factory, constructor = setup

                def under_lock(value):
                    def checked(*_):
                        self.assertTrue(state["held"])
                        return value
                    return checked

                client_factory.side_effect = under_lock(({}, website))
                config_factory.side_effect = under_lock(({}, website.config))
                constructor.side_effect = under_lock(website)
                website.start_run.side_effect = under_lock({"id": "r", "status": "RUNNING"})
                self.assertEqual(args.func(args), 0)
                self.assertFalse(state["held"])
                flock.assert_called_once()
                opened.assert_called_once()
                closed.assert_called_once_with(73)

    def test_busy_lock_blocks_before_client_config_and_writes_even_with_concurrency(self):
        for name in ("run", "start", "submit"):
            with self.subTest(command=name), mocked_lock(busy=True) as lock, self.command(name, ["--allow-concurrent"]) as setup:
                args, website, _, client_factory, config_factory, constructor = setup
                self.assert_reason("lock_busy", lambda: args.func(args))
                client_factory.assert_not_called()
                config_factory.assert_not_called()
                constructor.assert_not_called()
                self.assert_no_writes(website)
                lock[2].assert_called_once_with(73)

    def test_lock_released_on_preflight_failure(self):
        with mocked_lock() as lock, self.command("run") as setup:
            args, _, meter, *_ = setup
            meter.balance.return_value = balance(available_balance="0")
            self.assert_reason("insufficient_balance", lambda: args.func(args))
            self.assertFalse(lock[0]["held"])
            lock[2].assert_called_once_with(73)

    def test_explicit_concurrency_reaches_both_tasks(self):
        for name in ("run", "submit"):
            with self.subTest(command=name), mocked_lock(), self.command(name, ["--all-tasks", "--allow-concurrent"]) as setup:
                args, website, *_ = setup
                website.list_runs.return_value = [{"id": "other", "status": "RUNNING"}]
                self.assertEqual(args.func(args), 0)
                self.assertEqual(website.create_run.call_count, 2)
                self.assertEqual(website.start_run.call_count, 2)

    def test_unverified_submit_archive_is_never_started(self):
        with mocked_lock(), self.command("submit") as setup:
            args, website, *_ = setup
            website.upload.return_value["archive_verified"] = False
            with self.assertRaises(ApiError) as caught:
                args.func(args)
            self.assertEqual(caught.exception.details["reason"], "archive_unverified")
            website.upload.assert_called_once()
            website.create_run.assert_not_called()
            website.start_run.assert_not_called()

    def test_uncertain_run_write_stops_remaining_tasks(self):
        for failed_method in ("create_run", "start_run"):
            with self.subTest(method=failed_method), mocked_lock(), self.command("run", ["--all-tasks", "--allow-concurrent"]) as setup:
                args, website, *_ = setup
                getattr(website, failed_method).side_effect = ApiError("lost response", uncertain=True)
                self.assertEqual(args.func(args), 1)
                website.create_run.assert_called_once()
                self.assertLessEqual(website.start_run.call_count, 1)

    def test_submit_dry_run_skips_launch_checks_and_writes(self):
        with self.command("submit", ["--dry-run"]) as setup, patch.object(cli, "launch_lock") as lock:
            args, website, meter, *_ = setup
            self.assertEqual(args.func(args), 0)
            lock.assert_not_called()
            meter.balance.assert_not_called()
            self.assert_no_writes(website)


class QueuePreflightTests(SyntheticTestCase):
    def test_rejection_before_first_start_preserves_run_id(self):
        client = Mock(spec=OfficialClient)
        before = Mock(side_effect=PreflightError("insufficient_balance", "empty"))
        error = self.assert_reason("insufficient_balance", lambda: OfficialClient.start_run_with_queue_wait(
            client, "r", before_start=before,
        ))
        self.assertEqual(error.details["run_id"], "r")
        client.start_run.assert_not_called()

    def test_queue_retry_rechecks_and_stops_before_second_write(self):
        client = Mock(spec=OfficialClient)
        client.start_run.side_effect = RunStartError("r", 429, {"error": "capacity"})
        before = Mock(side_effect=[None, PreflightError("pending_billing", "unsettled")])
        with patch("arcbench_cli.client.time.monotonic", return_value=0), patch("arcbench_cli.client.time.sleep"):
            self.assert_reason("pending_billing", lambda: OfficialClient.start_run_with_queue_wait(
                client, "r", timeout_seconds=10, before_start=before,
            ))
        self.assertEqual(before.call_count, 2)
        client.start_run.assert_called_once_with("r")

    def test_explicit_upload_key_selects_meter_without_mutating_config(self):
        website = Mock(spec=OfficialClient)
        website.config = SubmitConfig(api_key="original", meter_cookie="synthetic-cookie")
        args = cli.build_parser().parse_args(["run", "s", "--task", "c--a"])
        with patch.object(cli, "_meter_from_config") as factory:
            check = cli._launch_preflight(args, website, api_key="selected")
            check.meter_factory()
        selected, selected_website = factory.call_args.args
        self.assertEqual(selected.api_key, "selected")
        self.assertEqual(selected.meter_cookie, "")
        self.assertIs(selected_website, website)
        self.assertEqual(website.config.api_key, "original")
        self.assertEqual(website.config.meter_cookie, "synthetic-cookie")


if __name__ == "__main__":
    unittest.main()
