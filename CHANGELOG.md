# Changelog

## 0.2.0 — consolidation

This release merges a second, newer ARC-Bench client that had been developed
separately inside a private harness repository. Before the merge the two were
assumed to hold the same capabilities; they did not. The standalone package
predates the other client's September additions, and the other client never had
the standalone's test-pack fetch, submission budget or queue wait.

Everything now lives in one command surface, on one credential model, with no
duplicate implementations.

### Capability diff before the merge

`—` means the capability was absent.

| Capability | Standalone package | In-repo client | Resolution |
|---|---|---|---|
| Log in | `session` — reads macOS Chrome cookies into an env file | `auth-import` — pipes or posts a cookie JSON into a private session file | Kept `session`; one credential model, one file. Non-macOS users set the variable by hand. |
| Check session | — (folded into `submit`) | `whoami` | Added `whoami`, with `--meter` for the metering service. |
| Metering balance | — | `balance` on a separate origin and session | Added, reading `ARC_BENCH_METER_COOKIE` from the same env file. |
| List competitions | `tasks` | `competitions` | `tasks` now takes an optional competition; `competitions` lists them. |
| List one competition's tasks | `tasks -v` | `tasks <competition>` | Both spellings work. |
| Fetch official test packs | `fetch` | — | Kept. |
| List submissions | — | `submissions` | Added. |
| List runs | `status` with no run id | `runs --task --submission` | Both; `runs` carries the filters. |
| Leaderboard | — | `leaderboard --competition --task --team` | Added, including the website's task-suffix normalisation. |
| Build a submission ZIP | `package` — hard-wired to one private harness layout | — | Generalised to `package --from DIR --out ZIP`; no external checkout, validates the entrypoint. |
| Save a submission | `submit` (create only) | `upload`, verifying the stored archive by download and SHA-256 | `upload` added; `submit` now verifies the archive the same way. |
| Model gateway key | required in the env file | read from `/api/auth/access-key` when unset | Both; the env file wins, the account key is the fallback, and either is registered for redaction. |
| Create a run | inside `submit` | `run SUBMISSION --task` | Added as its own command. |
| Start a PENDING run | `start --run-id`, waiting out HTTP 429 capacity | `start RUN_ID`, no queue wait | Kept the queue wait; both spellings of the id work. |
| Cancel a run | — | `cancel` | Added, reading the state after the request. |
| Read one run | `status --run-id` | `status` with `--full` | Merged; `--full` prints the redacted whole response. |
| Wait for a run | `status --wait` | `wait`, with exit codes 0/1/2 | Merged into `wait`, and `status RUN_ID` returns the same codes. |
| Read logs | `logs` — whole body to a file, extracts marked requirements | `logs --tail --offset` — bounded tail plus a cursor | Merged: bounded tail and cursor by default, `--out` also saves the body and extracts the markers. |
| Read one workspace file | — | `source` | Added; writes mode 600 and redacts. |
| Download the workspace bundle | — | `download` | Added. |
| Download a submission archive | — | `archive` | Added. |
| Replay an archived app locally | `replay` — shells out to a script in a private repository | — | **Removed.** See below. |

### Behaviour differences resolved

| Behaviour | Before | Now |
|---|---|---|
| Route-affinity cookie | The standalone sent a fixed `Cookie` header and dropped the server's updates | A cookie jar seeded from the env file keeps whatever the server hands back |
| Truncated HTTP body | Propagated as an unhandled exception | Reported as a transport failure, never as a run result |
| Polling through a failure | Gave up on the first error | Tolerates two consecutive transport or 5xx failures, then re-reads; a third gives up |
| Cross-origin redirect | Followed | Refused for any authenticated request |
| Uncertain writes | Not distinguished | Errors carry `outcome_uncertain` and name the id to inspect; no mutation is ever retried |
| Secret redaction | By field name only | By field name **and** by value, so a key quoted inside a log line or a source file is replaced too |
| Cost reporting | `token_cost_usd` read as USD | Amount and the response's explicit currency always reported together; observed values are CNY |
| Cancellation returning 500 | Treated as a failure | The run state is read afterwards and both are reported |
| Run summary | Two different shapes | One shape, including steps, failed tests and cost |
| Output format | Human lines in one client, JSON in the other | Human by default, `--json` for one compact record, accepted before or after the subcommand |

### Removed

* **`replay`.** It ran `scripts/replay.py` from a separate private repository.
  Outside that repository it could only ever fail, so it was dead weight in a
  client meant to stand alone. Local replay belongs in the harness that owns
  the replay script.
* **`auth-import` and its localhost login form.** A second credential store for
  the same session. `session` covers macOS; everywhere else the cookie is one
  environment variable.
* **The `cryptography` dependency.** Chrome's cookie blobs are now decrypted by
  a small bundled AES-128-CBC implementation pinned to the FIPS-197 and
  RFC 3602 vectors. The package has no third-party runtime dependencies.

### Other changes

* Nothing in the package references the private harness any more: no repository
  paths, team names, account ids, run ids, or competition material. The status
  fixture keeps the real field shapes with its identifiers replaced.
* `package`, `source`, `download` and `archive` refuse to overwrite an existing
  output path.
* `README.md` is bilingual and documents the full API route table, the config
  precedence, exit codes, and the queue and rate rules.
* The test suite grew from 19 checks to 76, including an in-process synthetic
  ARC-Bench server covering multipart encoding, cookie affinity, truncated
  bodies, redaction, redirect boundaries, archive verification, cancellation and
  queue waiting.

### Known blocker for a public release

`LICENSE` is an all-rights-reserved internal notice, not MIT. It must be
replaced by the copyright holder before this repository is made public.

## 0.1.0

Initial standalone package: `session`, `tasks`, `fetch`, `package`, `submit`,
`start`, `status`, `logs` and `replay`, with queue-capacity waiting on run
start and token metrics kept in submission records.
