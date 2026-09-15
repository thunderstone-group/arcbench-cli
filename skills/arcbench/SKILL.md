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
arcbench run SUBMISSION_ID --all-tasks          # or repeat --task; one run per task, overlapping
arcbench wait RUN_A RUN_B --interval 20 --timeout 3600
arcbench status RUN_ID --full          # or: logs RUN_ID --tail 40, source RUN_ID PATH --output F
arcbench leaderboard --competition ticket-booking --task ticket-booking
```

Account and gateway, neither needing a browser cookie — both log in to the
metering site with the gateway access key:

```sh
arcbench balance                       # account, balance, currency, as_of, pending events
arcbench models                        # every gateway model, provider and price
```

Notes per step: **upload** downloads the stored archive back and compares
SHA-256 against the local file — exit `1` means they don't match; the JSON record
still has `submission.id`, so inspect it rather than blindly re-uploading.
**run** creates and starts in two API calls per task and prints a JSON array,
one entry per task; if a start fails, that entry carries `run_id` and
`start_error` — the run exists, so resume with `arcbench start RUN_ID`, never
call `run` again (that creates a second run), and the remaining tasks still
start. **wait** takes several run ids, polls them round-robin, and with `--json`
emits one line per observed state change, each carrying its `run_id`. **status**
also takes several ids. **logs** takes `--offset` to continue from a cursor.

`arcbench submit` collapses upload/run/wait into one command and writes a JSON
result record under `--record-dir`. Prefer it for unattended use; it enforces
`ARC_BENCH_MAX_SUBMISSIONS` and `ARC_BENCH_MIN_INTERVAL_SECONDS`.

```sh
arcbench submit --package dist/agent.zip \
  --competition ticket-booking --task ticket-booking--ticket-booking --name my-agent-v3
arcbench submit --package dist/agent.zip --competition arc-bench-web --all-tasks
```

## Exit codes

`0` success (for `wait`, `status RUN_ID` and `submit`, every run PASSED); `1` a
request failed, a task failed to start, or a run reached a non-passing terminal
state; `2` the local waiting deadline expired, or the session is not logged in.
With several runs: `1` if any finished without passing, else `2` if any was still
going at the deadline. Two traps: `wait`
exiting `2` means the **local** deadline expired, not that the run failed — it is
still going, so re-enter `wait` or poll `status`. `status RUN_ID` exits `1` for
any run that is not PASSED, **including one still running** — read the `status`
field in the JSON, don't treat exit `1` as failure. `leaderboard` requires
`--competition` (task, team, and limit are optional filters).

## Platform rules the CLI cannot fully enforce — the agent must

- **Run concurrently by default.** The platform runs submissions and tasks at the
  same time. Measured 2026-09-15: two saved submissions for `arc-bench-web` held
  seven runs `RUNNING` at once — five tasks of one and two of the other — all past
  `deploy_agent`, none stranded. There is no rule against overlapping runs, and no
  rule against uploading while a run is pending; earlier versions of this skill
  said there was, and were wrong.
- **Serialize only when the cost figure matters.** The official token count is a
  usage-meter delta on the shared access key, so a second run, or the agent's own
  local gateway calls, inflate the token and cost figures of whatever is running.
  That affects the cost-efficiency ranking (senior tier, pass rate ≥ 80%) and
  nothing else; pass rate is unaffected.
- **The leaderboard shows the latest completed run per task, not the best one.**
  A competition score is the highest average across every saved submission.
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
leaderboard`，各步示例见上文英文部分，命令与参数完全一致。`run` 和 `submit` 的 `--task`
可重复、也可换成 `--all-tasks` 一次起完整个赛题；`wait` 和 `status` 能一次接多个 run id。
无人值守场景优先用 `arcbench submit`，一条命令收敛 upload/run/wait 并写 JSON 结果记录。

**账号与网关**：`arcbench balance` 和 `arcbench models` 用网关 access key 登录计量站，
**不需要浏览器 cookie**，分别给出余额/账期和全部模型的价格表。

**退出码**：`0` 成功；`1` 请求失败、有任务没启动成功，或有运行进入非通过终态；`2` 本地
等待超时或未登录。多个运行时：只要有一个跑完且没通过就是 `1`，否则只要有一个到点还没跑完
就是 `2`。`wait` 退 `2` 不代表运行失败，它还在跑。`status RUN_ID` 对任何非 PASSED（包括仍
在运行）的运行都退 `1`，要读 JSON 里的 `status` 字段而不是看退出码。`leaderboard` 必须带
`--competition`。

**平台真实规则**（CLI 不能替 agent 全部强制执行）：**默认就并发**——平台本身并发跑提交和
任务，2026-09-15 实测 `arc-bench-web` 下两份提交同时有七个运行在 `RUNNING`（一份五个任务、
另一份两个），全部越过 `deploy_agent` 且没有一个被挤掉；本 skill 早先写的「运行不能重叠」
「pending 时不能上传」两条都不成立，已删。**只有在乎成本数字时才串行**：官方 token 计数是
共享 access key 上的计量差值，并发的运行和 agent 自己的本地网关调用都会被算进去，这只影响
性价比榜（senior 档，通过率 ≥ 80%），对通过率没有影响。排行榜展示的是每个任务最近一次
**完成**的运行，赛题总分取所有已保存提交里最高的那个平均值。

**绝对不能做**：量 token 的运行旁边继续花这把 key；报错后盲目重试写请求（`upload`/`run`/
`start`/`cancel`，改读 `outcome_uncertain` 给出的 id 状态再决定）；打印、记录或提交凭据。

**参考**：精确参数见 `arcbench <command> --help`；完整说明见仓库 README 的
「Agent 接入」与「排队与频率约束」两节。
