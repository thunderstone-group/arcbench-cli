# VOLO launch preflight hardening

## Scope

Date: 2026-09-16. Branch: `fix/volo-ops-hardening`.

Continued the existing uncommitted changes in `cli.py`, `client.py` and
`preflight.py`. Added `tests/test_preflight.py` and updated both language sections
of the README. The synthetic checks found no additional confirmed implementation
bug; the inherited Python implementation was retained. The delegate made no commit, push or merge.

## Actual validation

Executed from the worktree with the existing Python 3.11 interpreter, without
installing anything:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3.11 -m unittest discover -s tests -p 'test_preflight.py' -v
```

Result: exit code `0`; `Ran 29 tests in 1.034s`; `OK`, with no skips or failures.
These are synthetic fixtures built with `unittest.mock`, not captured upstream
responses or evidence of live API compatibility.

Also ran `git diff --check`: no whitespace errors reported in tracked changes.
Final status contains only the inherited three Python files, the README, the new
mock test module and this report.

Coverage:

* Positive, zero, negative, unknown and below-minimum balances; invalid minimums
  and currencies; unavailable meter, pending/invalid billing counts and invalid
  timestamps.
* Pending, queued, running, terminal, malformed and unavailable run lists;
  exemption of the target pending run; default single task and explicit overlap.
* Initial preflight rejection in `cmd_run`, `cmd_start` and `cmd_submit` makes
  no upload, create or start call. Repeated tasks and `--all-tasks` reject before
  writes without explicit concurrency. Non-pending `start` also rejects.
* The real POSIX lock context manager with mocked filesystem/OS calls: lock held
  before `_client` for run/start and before `_config`/client construction for
  submit, held through start, released on success/error. Busy lock blocks all
  three commands before initialization, even with `--allow-concurrent`.
* Fresh meter/run reads on repeated checks, rechecking before queue retries,
  rejection before the first/second start write, and preservation of the run id.
* Explicit concurrency launches both synthetic tasks; uncertain writes stop
  later run tasks; unverified submit archives never start; dry-run avoids launch
  checks/writes; an explicit upload key selects its meter without changing the
  original configuration.

Tests block socket creation, real HTTP transport, env-file loading and browser
cookie capture. Configurations, package validation, budgets, record writes,
clients and lock system calls are mocked. No port was opened, external request
sent, browser credential read or competition mutation performed.

## Checks deliberately not run

* Existing test modules and full discovery, including local HTTP integration
  tests and old long-running checks.
* Live meter/competition calls, real uploads/runs, browser sessions or credentials.
* Real multi-process OS lock contention, Windows locking, cross-machine behavior,
  and other Python versions. The lock tests validate mocked POSIX control flow.
* Dependency/network installation, builds, CI, publication or deployment.

## Compatibility changes in the inherited patch

* `run`, `start` and non-dry-run `submit` now require a valid positive meter
  balance, zero pending billing events, a parseable timestamp and a readable
  complete run list before launching. Errors fail closed. Existing launch scripts
  now need working meter credentials in addition to a website session.
* Default launches allow one task and reject other pending/active runs. Scripts
  selecting multiple tasks must explicitly pass `--allow-concurrent`, accepting
  contaminated per-run token/cost accounting. This flag does not bypass meter
  checks or the local lock.
* `--min-balance` is a threshold in the reported meter currency, not a fee budget,
  reservation or spending cap. Timestamps are parsed but have no maximum-age
  policy. A passing preflight cannot guarantee sufficient funds for completion.
* The lock is per local OS user at `~/.cache/arcbench/launch.lock`, independent of
  checkout/config/record paths, and is held for the entire command (including
  submit polling). It is not a cross-machine global lock and does not coordinate
  web/API callers or other OS users. Runs can outlive their launching command.
* `start` requires a confirmed pending target. Checks repeat before later creates
  and every start attempt, including capacity retries; rejection cannot undo
  earlier writes. `submit` no longer starts an unverified uploaded archive.
* `list_runs(limit=None)` returns all rows supplied by the endpoint without local
  truncation; malformed non-list responses now raise. This adds no server-side
  pagination. Invalid meter response envelopes also raise.
* Incomplete create/start responses are reported as uncertain writes. `run`
  stops subsequent tasks after an uncertain outcome. The queue helper accepts an
  optional `before_start` callback and preserves the run id on preflight errors.
* Successful launch output/records include a `preflight` snapshot. Standalone
  `upload` retains its existing behavior; `submit --dry-run` skips launch checks
  and locking but still performs its existing login/task reads.

This validation is limited to the mock behaviors above; no live integration or
full-suite compatibility result is claimed.

## Coordinator disposition

The coordinator reviewed the launch/client diff and reran all 29 new mock tests: OK
(1.046 seconds). This is a DRAFT integration, not a released CLI. The existing 95-test
baseline and real transport/lock compatibility remain unverified for this patch, so
main and the installed CLI are intentionally unchanged. Two historical pi-harness
PENDING records on the account will also block default launches until safely resolved.
No cancellation bypass or paid operation is part of this patch.
