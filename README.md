# arcbench-cli

A small command-line client for [ARC-Bench](https://arc-bench.com). It talks to
the same HTTP API the website's own front end calls: discover competitions,
download official test packs, upload an agent, start and watch runs, and read
logs, source files and leaderboards.

It is a **pure HTTP client**. Exactly one command, `arcbench session`, touches
the browser, and only once per login, to read the cookie the platform already
gave you. Everything after that is `urllib` against `https://arc-bench.com/api`.

* No third-party runtime dependencies. Python 3.10+ and the standard library.
* No browser automation, no driver, no headless Chrome.
* Credentials live in one env file and are never printed, logged or recorded.

## Why not drive the website?

The obvious alternative is to drive `arc-bench.com` through a real browser over
the Chrome DevTools Protocol. That works, and it is slow and brittle:

| | This CLI | Browser automation |
|---|---|---|
| Reading a run's status | one GET, tens of milliseconds | page load, hydrate, scrape the DOM |
| Watching a run to completion | one polled GET per interval | a live tab held open for the whole run |
| Failure modes | HTTP status codes | selector drift, hydration races, redraws |
| Running unattended | a cron job or a CI step | a display, a browser and a session that dies quietly |
| Parsing | the platform's own JSON | whatever the markup happens to render today |

A DOM is a rendering of the data. This client reads the data. The one thing a
browser genuinely owns is the login, and that is the one thing `session`
borrows from it.

## Install

```sh
pipx install git+https://github.com/thunderstone-group/arcbench-cli.git
```

Or from a checkout:

```sh
git clone https://github.com/thunderstone-group/arcbench-cli.git
cd arcbench-cli
pip install -e .
```

## Logging in

Sign in at <https://arc-bench.com/login> in Chrome, then:

```sh
arcbench session                 # writes ARC_BENCH_SESSION_COOKIE into ./.env
arcbench session --profile "Profile 1"
arcbench whoami                  # confirms the session is live
```

`session` copies Chrome's cookie database to a temporary file (so Chrome does
not need to be closed), asks the macOS keychain for the *Chrome Safe Storage*
key, decrypts the two ARC-Bench cookies, and writes them into an env file with
mode `600`. The value is never printed. Approve the keychain prompt when it
appears. Re-run it when the session expires.

**On Linux and Windows**, `arcbench session` does not run. Open the site's dev
tools, copy the `Cookie` request header, and set the variable by hand:

```sh
ARC_BENCH_SESSION_COOKIE='arcbench_session=...; arc_api_sticky=...'
```

Both cookies matter: `arc_api_sticky` is the platform's route-affinity cookie,
and the client keeps whatever the server hands back during a session.

## Configuration

Values are read from the first env file found, in this order:

1. `--env-file PATH`
2. `$ARCBENCH_ENV_FILE`
3. `./.env`
4. `~/.config/arcbench/.env`

Real environment variables beginning with `ARC_BENCH_` override the file, so CI
can supply credentials without writing one. `--base-url` overrides
`ARC_BENCH_WEB_BASE_URL`. See [`.env.example`](.env.example) for every setting.

Global flags work before or after the subcommand. `--json` prints one compact
JSON record per command, which is the form to use from a script or an agent.

## Commands

Discovery:

```sh
arcbench competitions                          # every open competition
arcbench tasks ticket-booking                  # one competition's tasks
arcbench fetch --competition ticket-booking    # official test packs to ./testsets/official
arcbench leaderboard --competition ticket-booking --task ticket-booking
arcbench submissions --competition ticket-booking
arcbench runs --limit 10 --task ticket-booking--ticket-booking
```

Account:

```sh
arcbench whoami
arcbench balance                               # needs ARC_BENCH_METER_COOKIE
```

Submitting and running:

```sh
arcbench package --from ./agent --out dist/agent.zip
arcbench upload dist/agent.zip --competition ticket-booking --name my-agent-v3
arcbench run SUBMISSION_ID --task ticket-booking--ticket-booking
arcbench start RUN_ID                          # start a run that is still PENDING
arcbench cancel RUN_ID
```

Or all of it in one command, which also writes a JSON result record:

```sh
arcbench submit --package dist/agent.zip \
  --competition ticket-booking --task ticket-booking--ticket-booking \
  --name my-agent-v3
```

Watching and inspecting:

```sh
arcbench status                                # recent runs
arcbench status RUN_ID                         # one run's summary
arcbench status RUN_ID --full                  # the whole redacted response
arcbench wait RUN_ID --interval 10 --timeout 1200
arcbench logs RUN_ID --tail 40
arcbench logs RUN_ID --offset 22296 --tail 40  # continue from the last cursor
arcbench source RUN_ID backend/package.json --output ./out/package.json
arcbench download RUN_ID --output ./out/workspace.zip
arcbench archive SUBMISSION_ID --output ./out/submitted.zip
```

`source` paths are relative to the generated template root, so `backend/...`
rather than `template/backend/...`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success; for `wait`, `status RUN_ID` and `submit`, the run PASSED |
| 1 | a request failed, or the run reached a non-passing terminal state |
| 2 | the local waiting deadline expired, or the session is not logged in |

A `wait` that times out does **not** cancel the remote run.

## Queue and rate etiquette

The platform runs submissions on shared capacity, and its official token count
is a usage-meter delta taken across a run. Two rules follow, and the client is
built around them:

1. **Never let two runs on one account overlap.** The meter delta cannot tell
   two concurrent runs apart, so overlapping runs corrupt both token counts.
   Finish or cancel a run before starting the next.
2. **Never create another upload while a run from the previous submission is
   still pending.** The server keeps the latest saved submission for a
   competition, so a new upload can strand a run you are waiting on.

Behaviour that follows from those rules:

* Creating a run and starting it are separate API calls. If `/start` is refused
  for capacity (HTTP 429), the client waits and retries **the same run id**,
  honouring `Retry-After` with a small backoff and jitter, until
  `--queue-timeout` expires. It never creates a second run or a second upload.
* A mutation is never retried automatically. When a write's outcome cannot be
  observed, the error says `outcome_uncertain` and names the id to inspect.
* `ARC_BENCH_MAX_SUBMISSIONS` and `ARC_BENCH_MIN_INTERVAL_SECONDS` cap and pace
  how many real submissions `submit` will create from one machine.
* A truncated HTTP body is reported as a transport failure, never as a run
  result. `wait` tolerates two consecutive transport or 5xx failures, then
  re-reads; a third gives up rather than guessing.

## API routes

The contract below was read from the deployed front-end bundle and verified
against the live service. The bundle is not vendored.

| Operation | Method and route |
|---|---|
| Check session | `GET /api/auth/me` |
| Account model key | `GET /api/auth/access-key` |
| List competitions | `GET /api/competitions` |
| Competition detail and tasks | `GET /api/competitions/{id}` |
| Leaderboard | `GET /api/competitions/leaderboard?track=all&competition_id=…&task_id=…` |
| Official test pack | `GET /api/requirements/{task_id}/tests?catalog=competition` |
| List submissions | `GET /api/submissions` |
| Save submission | `POST /api/submissions` (multipart: competition_id, runtime, catalog, agent_source, display_name, base_url, api_key, model, file) |
| Download submission archive | `GET /api/submissions/{id}/archive` |
| Create run | `POST /api/runs` (multipart: submission_id, requirement_id) |
| Start run | `POST /api/runs/{id}/start` |
| Cancel run | `POST /api/runs/{id}/cancel` |
| Read run | `GET /api/runs/{id}` |
| List runs | `GET /api/runs?requirement_id=…&submission_id=…` |
| Run logs | `GET /api/runs/{id}/logs?log_offset=N` |
| One workspace file | `GET /api/runs/{id}/source?file_path=…&kind=file` |
| Generated workspace bundle | `GET /api/runs/{id}/workspace/template-bundle` |
| Meter session | `GET /api/user/me` (on the meter origin) |
| Meter balance | `GET /api/user/balance` |
| Meter freshness | `GET /api/user/freshness` |

Two observed quirks the client handles rather than papers over. The cost field
is named `token_cost_usd` but carries an explicit `token_cost_currency` that is
not always USD, so amount and currency are always reported together. A
cancellation can return HTTP 500 *after* it has taken effect, so `cancel` reads
the run state afterwards and reports both.

## Security

* Credentials are read from the env file only. Nothing writes them back out.
* Records and printed output are scrubbed twice: by field name, and by value,
  so an access key quoted inside a log line or a workspace file is replaced too.
* Files written by `source` are created with mode `600`, and no command
  overwrites an existing output path.
* An authenticated request is never followed to another origin.
* The metering service's cookies and the website's cookies are never mixed.

## Development

```sh
python -m unittest discover -s tests -v
```

The suite runs a synthetic ARC-Bench server in-process and checks the contract
that matters: multipart encoding, cookie affinity, truncated bodies, redaction,
redirect boundaries, archive verification, cancellation, and waiting through a
queue. AES decryption is pinned to the FIPS-197 and RFC 3602 vectors. No test
touches the network.

## License

MIT. See [`LICENSE`](LICENSE).

---

# arcbench-cli（中文）

[ARC-Bench](https://arc-bench.com) 的命令行客户端。它调用网站前端用的同一套 HTTP
接口：查看赛题、下载官方测试包、上传 agent、发起和跟踪运行、读取日志、源文件和
排行榜。

这是一个**纯 HTTP 客户端**。只有 `arcbench session` 一条命令会接触浏览器，而且每次
登录只需一次，用来读取平台已经发给你的 cookie。之后所有请求都是 `urllib` 直接打
`https://arc-bench.com/api`。

* 运行时零第三方依赖，只需 Python 3.10+ 和标准库。
* 不需要浏览器自动化、driver 或无头 Chrome。
* 凭据只存在一个 env 文件里，不会被打印、写日志或记进结果文件。

## 为什么不直接驱动网页

另一条路是用 Chrome DevTools Protocol 操作 `arc-bench.com`。那条路能走通，但又慢又脆：

| | 本 CLI | 浏览器自动化 |
|---|---|---|
| 读一次运行状态 | 一个 GET，几十毫秒 | 加载页面、hydrate、再从 DOM 里抠 |
| 跟踪一次运行到结束 | 每个间隔一个 GET | 整个运行期间挂着一个活标签页 |
| 失败形态 | HTTP 状态码 | 选择器漂移、hydration 竞态、重绘 |
| 无人值守运行 | 一个 cron 或 CI 步骤 | 要显示器、要浏览器，会话还会悄悄死掉 |
| 解析对象 | 平台自己的 JSON | 今天恰好渲染成什么样的 HTML |

DOM 是数据的一次渲染，本客户端直接读数据。浏览器真正独占的只有登录这一件事，而
`session` 借的正是这一件事。

## 安装

```sh
pipx install git+https://github.com/thunderstone-group/arcbench-cli.git
```

## 登录

先在 Chrome 里登录 <https://arc-bench.com/login>，然后：

```sh
arcbench session                 # 把 ARC_BENCH_SESSION_COOKIE 写进 ./.env
arcbench whoami                  # 确认会话有效
```

`session` 会把 Chrome 的 cookie 数据库复制到临时文件（所以不必关掉 Chrome），向
macOS 钥匙串要 *Chrome Safe Storage* 密钥，解密两个 ARC-Bench cookie，再以 `600`
权限写进 env 文件。cookie 值不会被打印。钥匙串弹窗出现时请点允许。会话过期后重跑。

**Linux 和 Windows 上没有这条路**：打开网站开发者工具，复制 `Cookie` 请求头，手工
设置环境变量即可。

```sh
ARC_BENCH_SESSION_COOKIE='arcbench_session=...; arc_api_sticky=...'
```

两个 cookie 都要带上：`arc_api_sticky` 是平台的路由亲和 cookie，客户端会在会话期间
保留服务端回写的新值。

## 配置优先级

按顺序取第一个存在的 env 文件：`--env-file PATH` → `$ARCBENCH_ENV_FILE` → `./.env`
→ `~/.config/arcbench/.env`。以 `ARC_BENCH_` 开头的真实环境变量优先级高于文件，方便
CI 不落盘提供凭据。全局参数放在子命令前后都可以；`--json` 输出单行紧凑 JSON，脚本和
agent 用这个。

## 命令一览

命令、示例与退出码见上文英文部分，行为完全一致：

* 查看类：`competitions`、`tasks`、`fetch`、`leaderboard`、`submissions`、`runs`
* 账号类：`whoami`、`balance`
* 提交类：`package`、`upload`、`run`、`start`、`cancel`、`submit`
* 跟踪类：`status`、`wait`、`logs`、`source`、`download`、`archive`

退出码：`0` 成功（`wait`、`status RUN_ID`、`submit` 表示运行 PASSED）；`1` 请求失败
或运行进入非通过的终态；`2` 本地等待超时，或会话未登录。**本地 `wait` 超时不会取消
远端运行。**

## 排队与频率约束

平台在共享算力上跑提交，而官方 token 计数是运行前后在计量表上取的差值。由此有两条
硬性规则，客户端整个是围绕它们设计的：

1. **同一账号的两次运行绝不能重叠**。计量差值分不清两个并发运行，重叠会同时污染两边
   的 token 计数。先跑完或取消上一个，再开下一个。
2. **上一份提交还有运行在 pending 时，不要再传新的包**。服务端只保留某个赛题最新保存
   的提交，新上传可能让你正在等的运行失效。

由此而来的行为：

* 创建运行和启动运行是两个独立请求。`/start` 因容量被拒（HTTP 429）时，客户端只对
  **同一个 run id** 重试，按 `Retry-After` 加小幅退避和抖动等待，直到 `--queue-timeout`
  用完。它不会再建第二个运行，也不会再传第二份包。
* 写请求一律不自动重试。无法确认写入结果时，错误里会带 `outcome_uncertain`，并给出
  应该去查的 id。
* `ARC_BENCH_MAX_SUBMISSIONS` 和 `ARC_BENCH_MIN_INTERVAL_SECONDS` 限制并放缓单机
  `submit` 能创建的真实提交数量。
* HTTP 响应体被截断一律算传输失败，绝不当成运行结果。`wait` 容忍连续两次传输或 5xx
  失败后重读，第三次就放弃而不是猜。

## 接口路由表

见上文英文部分的表格，内容一致。另有两个已观测到的怪异之处，客户端是如实处理而不是
掩盖：费用字段名叫 `token_cost_usd`，但同时返回的 `token_cost_currency` 并不总是 USD，
所以金额和币种永远一起输出；取消请求可能在**已经生效之后**返回 HTTP 500，所以 `cancel`
会在之后再读一次运行状态，两者一并报告。

## 安全

凭据只从 env 文件读入，不会被写回。输出和结果文件会做两层擦除：按字段名，以及按取值，
所以被引用在日志行或工作区文件里的 access key 同样会被替换。`source` 写出的文件权限是
`600`，任何命令都不会覆盖已存在的输出路径。认证请求不会被跟随到另一个 origin。计量服务
和网站的 cookie 互不混用。

## 开发

```sh
python -m unittest discover -s tests -v
```

测试会在进程内跑一个合成的 ARC-Bench 服务器，覆盖真正容易出错的契约：multipart 编码、
cookie 亲和、截断响应、脱敏、跨 origin 重定向边界、归档校验、取消、以及排队等待。AES
解密对齐 FIPS-197 与 RFC 3602 测试向量。没有任何测试会走真实网络。

## 许可

MIT，见 [`LICENSE`](LICENSE)。
