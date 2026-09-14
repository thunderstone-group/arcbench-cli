---
name: arcbench
description: Drive ARC-Bench (arc-bench.com) end to end through the arcbench CLI — package and upload an agent submission, start and watch a run, read logs/status/source, and check leaderboards. Use when the task involves ARC-Bench, arc-bench.com, submitting or evaluating a coding agent on the platform, or Chinese trigger words such as 提交 harness, 跑 ARC-Bench, 查榜, 排行榜, 看 run 日志, 上传 agent.
---

# arcbench

`arcbench` is a pure HTTP client for [ARC-Bench](https://arc-bench.com). It has no
browser automation; every command after login is a plain HTTP request. Use it to
package an agent, submit it, watch the run, and read back logs, source files and
the leaderboard.

## Install

```sh
uv tool install git+https://github.com/thunderstone-group/arcbench-cli.git
# or: pipx install git+https://github.com/thunderstone-group/arcbench-cli.git
```

## One-time human login (cannot be done by an agent)

A person signs in to <https://arc-bench.com/login> in Chrome on a Mac, then runs:

```sh
arcbench session          # writes ARC_BENCH_SESSION_COOKIE, raises a keychain prompt
arcbench whoami            # confirms the session is live
```

This step is interactive (keychain prompt) and out of scope for an agent. After it
exists, only point the agent at the resulting env file, kept outside any repo:

```sh
export ARCBENCH_ENV_FILE=~/.config/arcbench/team.env
```

Every command reads credentials from that file. Never pass a cookie or model key as
a command-line argument, and never print or commit the env file.

## Always use `--json`

Every command with `--json` prints one compact JSON record (or array) to stdout.
Do not parse the default human-readable output; it can change. Two stream traps:
diagnostic lines (e.g. a queue-full notice) also go to stdout but are always
prefixed `[arcbench] ` — parse each stdout line as JSON and skip lines with that
prefix; and when a request fails, the error record goes to **stderr**, not
stdout, so capture both streams.

## The loop

```
package → upload (verify hash) → run → wait → status / logs / source → leaderboard
```

```sh
arcbench package --from ./agent --out dist/agent.zip
arcbench upload dist/agent.zip --competition ticket-booking --name my-agent-v3
arcbench run SUBMISSION_ID --task ticket-booking--ticket-booking
arcbench wait RUN_ID --interval 20 --timeout 3600
arcbench status RUN_ID --full          # or: logs RUN_ID --tail 40, source RUN_ID PATH --output F
arcbench leaderboard --competition ticket-booking --task ticket-booking
```

Notes per step: **upload** downloads the stored archive back and compares
SHA-256 against the local file — exit `1` means they don't match; the JSON record
still has `submission.id`, so inspect it rather than blindly re-uploading.
**run** creates and starts in two API calls; if start fails, the record carries
`run_id` and `start_error` — the run exists, so resume with `arcbench start
RUN_ID`, never call `run` again (that creates a second run). **wait** with
`--json` emits one line per observed state change, so an agent can stream
progress. **logs** takes `--offset` to continue from a cursor.

`arcbench submit` collapses upload/run/wait into one command and writes a JSON
result record under `--record-dir`. Prefer it for unattended use; it enforces
`ARC_BENCH_MAX_SUBMISSIONS` and `ARC_BENCH_MIN_INTERVAL_SECONDS`.

```sh
arcbench submit --package dist/agent.zip \
  --competition ticket-booking --task ticket-booking--ticket-booking --name my-agent-v3
```

## Exit codes

`0` success (for `wait`, `status RUN_ID` and `submit`, the run PASSED); `1` a
request failed, or the run reached a non-passing terminal state; `2` the local
waiting deadline expired, or the session is not logged in. Two traps: `wait`
exiting `2` means the **local** deadline expired, not that the run failed — it is
still going, so re-enter `wait` or poll `status`. `status RUN_ID` exits `1` for
any run that is not PASSED, **including one still running** — read the `status`
field in the JSON, don't treat exit `1` as failure. `leaderboard` requires
`--competition` (task, team, and limit are optional filters).

## Platform rules the CLI cannot fully enforce — the agent must

- **Never let two runs on one account overlap**, including a local model gateway
  call during an official run. The official token count is a usage-meter delta on
  the shared access key; concurrent traffic corrupts it. Finish or `cancel` a run
  before starting another.
- **Never upload while a run from the same submission is still pending.** The
  server keeps only the latest saved submission per competition, so a new upload
  can strand a run you're waiting on. Finish or `cancel` first.
- **The leaderboard shows the latest passing run, not the best one.**
- **Never retry a mutating call** (`upload`, `run`, `start`, `cancel`) after an
  error. When the outcome can't be observed, the error carries `outcome_uncertain`
  and the id to inspect — read that id's state and decide, don't repeat the call.
  The one built-in automatic retry is capacity backoff inside `run`/`start`, on
  the same run id.
- **Never print, log, or commit a credential.** Output is already redacted; don't
  work around it.

## Reference

Run `arcbench <command> --help` for exact flags; the CLI's own `--help` output is
the source of truth. Full behavior, API routes, and the queueing rationale are in
the [arcbench-cli README](https://github.com/thunderstone-group/arcbench-cli#readme),
sections "Agent integration" and "Queue and rate etiquette".

---

# arcbench（中文）

`arcbench` 是 [ARC-Bench](https://arc-bench.com) 的纯 HTTP 客户端，登录之后的每条
命令都是普通 HTTP 请求，没有浏览器自动化。用它打包 agent、提交、跟踪运行，并读回
日志、源文件和排行榜。

**安装**：`uv tool install git+https://github.com/thunderstone-group/arcbench-cli.git`

**登录（只需人做一次，agent 做不了）**：由人在已登录网站的 Mac 上跑 `arcbench
session`（会弹钥匙串授权），之后把结果 env 文件路径交给 agent：
`export ARCBENCH_ENV_FILE=~/.config/arcbench/team.env`。凭据只从该文件读取，永远
不要把 cookie 或模型 key 写进命令行参数或提交进仓库。

**永远加 `--json`**：每条命令输出一行紧凑 JSON。诊断信息同样走 stdout 但带
`[arcbench] ` 前缀，按行跳过；请求失败时错误记录在 **stderr**，两个流都要收。

**主循环**：`package → upload（校验哈希） → run → wait → status/logs/source →
leaderboard`，各步示例见上文英文部分，命令与参数完全一致。无人值守场景优先用
`arcbench submit`，一条命令收敛 upload/run/wait 并写 JSON 结果记录。

**退出码**：`0` 成功；`1` 请求失败或运行进入非通过终态；`2` 本地等待超时或未登录。
`wait` 退 `2` 不代表运行失败，它还在跑。`status RUN_ID` 对任何非 PASSED（包括仍在
运行）的运行都退 `1`，要读 JSON 里的 `status` 字段而不是看退出码。`leaderboard`
必须带 `--competition`。

**平台三条硬规则**（CLI 不能替 agent 全部强制执行）：同一账号的两次运行绝不能重
叠（token 计数是共享 key 上的计量差值）；上一份提交还有运行 pending 时不要再传新
包；排行榜展示的是最近一次通过的运行，不是最好的那次。

**绝对不能做**：同一 key 上并行做两件事；运行 pending 时上传；报错后盲目重试写请
求（`upload`/`run`/`start`/`cancel`，改读 `outcome_uncertain` 给出的 id 状态再决
定）；打印、记录或提交凭据。

**参考**：精确参数见 `arcbench <command> --help`；完整说明见仓库 README 的
「Agent 接入」与「排队与频率约束」两节。
