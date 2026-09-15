"""Fail-closed CLI launch checks over the existing meter and run contracts."""

from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .client import ApiError, OfficialClient, TERMINAL_STATES, parse_iso


class PreflightError(ApiError):
    def __init__(self, reason: str, message: str, **context) -> None:
        super().__init__(message)
        self.details.update(phase="preflight", reason=reason, **context)


def launch_lock_path() -> Path:
    # One lock per OS user, independent of checkout, env file and record dir.
    # No credential or account identifier is persisted in it.
    return Path.home() / ".cache" / "arcbench" / "launch.lock"


@contextmanager
def launch_lock():
    path = launch_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        raise PreflightError("lock_unavailable", "cannot open the local launch lock", lock_path=str(path)) from None
    try:
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError) as error:
            busy = isinstance(error, OSError) and error.errno in (errno.EACCES, errno.EAGAIN)
            raise PreflightError(
                "lock_busy" if busy else "lock_unavailable",
                "another CLI launch holds the local lock" if busy else "cannot acquire the local launch lock",
                lock_path=str(path),
            ) from None
        yield
    finally:
        # Closing releases the OS lock, including on errors. Never unlink it:
        # that would let another process lock a different inode at this path.
        os.close(descriptor)


class LaunchPreflight:
    def __init__(self, website: OfficialClient, meter_factory, minimum="0", allow_concurrent=False):
        try:
            self.minimum = Decimal(str(minimum))
        except InvalidOperation:
            self.minimum = Decimal("NaN")
        if not self.minimum.is_finite() or self.minimum < 0:
            raise PreflightError("invalid_min_balance", "--min-balance must be a finite non-negative decimal")
        self.website = website
        self.meter_factory = meter_factory
        self.meter = None
        self.allow_concurrent = allow_concurrent

    def check(self, *, target_run: str | None = None, task_count: int = 1) -> dict:
        if task_count > 1 and not self.allow_concurrent:
            raise PreflightError(
                "concurrent_tasks", "multiple tasks overlap; use one task or explicitly pass --allow-concurrent"
            )
        try:
            if self.meter is None:
                self.meter = self.meter_factory()
            snapshot = self.meter.balance()
        except (ApiError, RuntimeError, ValueError, OSError):
            raise PreflightError("balance_unavailable", "cannot query the meter; no launch is authorized") from None
        if not isinstance(snapshot, dict):
            raise PreflightError("balance_unknown", "meter returned no balance snapshot")
        try:
            amount = Decimal(str(snapshot.get("available_balance")))
        except InvalidOperation:
            amount = Decimal("NaN")
        currency = snapshot.get("currency")
        if not amount.is_finite() or not isinstance(currency, str) or not currency.strip():
            raise PreflightError("balance_unknown", "meter balance or currency is missing or invalid")
        if amount <= 0 or amount < self.minimum:
            raise PreflightError(
                "insufficient_balance", "meter balance must be positive and meet --min-balance",
                available_balance=str(amount), min_balance=str(self.minimum), currency=currency,
            )
        pending = snapshot.get("pending_billing_events")
        if type(pending) is not int or pending < 0:
            raise PreflightError("meter_unknown", "pending billing event count is missing or invalid")
        if pending:
            raise PreflightError("pending_billing", "billing has not settled", pending_billing_events=pending)
        try:
            stamp = snapshot.get("as_of")
            if not isinstance(stamp, str):
                raise ValueError("missing timestamp")
            parse_iso(stamp)
        except ValueError:
            raise PreflightError("meter_unknown", "meter freshness timestamp is missing or invalid") from None
        try:
            runs = self.website.list_runs(limit=None)
        except (ApiError, RuntimeError, ValueError, OSError):
            raise PreflightError("runs_unavailable", "cannot check the account's active runs") from None
        if not isinstance(runs, list):
            raise PreflightError("runs_unknown", "run list is not a list")
        active = []
        for run in runs:
            if not isinstance(run, dict) or not run.get("id") or not isinstance(run.get("status"), str):
                raise PreflightError("runs_unknown", "run list contains an incomplete run")
            state = run["status"].upper()
            if state not in TERMINAL_STATES | {"PENDING", "QUEUED", "RUNNING"}:
                raise PreflightError("runs_unknown", "run list contains an unknown state")
            if str(run["id"]) == target_run and state == "PENDING":
                continue
            if state not in TERMINAL_STATES:
                active.append(str(run["id"]))
        if active and not self.allow_concurrent:
            raise PreflightError("active_runs", "other pending or active runs can contaminate metering", run_ids=active)
        return {
            "available_balance": str(amount), "currency": currency,
            "min_balance": str(self.minimum), "pending_billing_events": pending,
            "as_of": snapshot.get("as_of"), "allow_concurrent": self.allow_concurrent,
            "active_run_ids": active,
        }
