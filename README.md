# ARC-Bench CLI

`arcbench-cli` 把 ARC-Bench 的比赛发现、测试包下载、候选 Harness 提交、运行轮询和本地重放收口成一个命令。

它是 `arcbench-harness-lab` 的配套工具，不包含 Harness 源码、官方测试数据、模型密钥或实验产物。平台命令可以独立运行；只有 `package` 和 `replay` 需要指向一个本地 lab checkout。

## 安装

需要 Python 3.10+。推荐用 `pipx`：

```bash
pipx install git+https://github.com/thunderstone-group/arcbench-cli.git
```

从源码开发：

```bash
git clone https://github.com/thunderstone-group/arcbench-cli.git
cd arcbench-cli
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
arcbench --version
```

`session` 当前支持 macOS Chrome。其余命令可在 macOS/Linux 上运行。

## 配置

先登录 [ARC-Bench](https://arc-bench.com/)，再从本机 Chrome 提取登录态：

```bash
arcbench session
```

它只会写入当前目录的 `.env`，不会打印 Cookie。也可以指定文件：

```bash
arcbench session --env ~/.config/arcbench/.env
```

配置查找顺序：

1. `ARCBENCH_ENV_FILE`
2. 当前目录 `.env`
3. 源码仓根目录 `.env`
4. `~/.config/arcbench/.env`
5. 进程环境变量 `ARC_BENCH_*`

不要把 `.env` 提交到 Git。真实环境变量优先于文件。

核心配置：

```dotenv
ARC_BENCH_SESSION_COOKIE=
ARC_BENCH_API_KEY=
ARC_BENCH_MODEL=deepseek-v4-flash
ARC_BENCH_MAX_SUBMISSIONS=4
ARC_BENCH_MIN_INTERVAL_SECONDS=30
```

`ARC_BENCH_API_KEY` 是 Runner 调模型网关用的 key，不是 ARC-Bench 登录 Cookie。两者都不得提交或回显。

## 命令与输入输出

通用形式：

```text
arcbench <command> [options]
```

| 命令 | 输入 | 输出/副作用 |
| --- | --- | --- |
| `arcbench session` | Chrome profile、env 路径 | 更新本地 `.env` 中的登录 Cookie |
| `arcbench tasks` | 无或 `-v` | 列出竞赛、任务数、测试数和状态 |
| `arcbench fetch` | `--competition <id>` | 下载公开测试包到 `./testsets/official/<competition>/` |
| `arcbench package` | `--lab-root <path>` | 打包 Harness ZIP，并输出大小和 SHA-256 |
| `arcbench submit` | ZIP、竞赛、任务、模型、名称 | 创建提交与运行，轮询进度，写 JSON 结果记录 |
| `arcbench status` | 无 `--run-id` 时列最近运行；有则读取一次 | 输出状态和 `(通过率, Token, 时间)` |
| `arcbench logs` | `--run-id <id>` | 保存原始日志，并抽取 `REQUIREMENTS-BEGIN/END` 内容 |
| `arcbench replay` | 归档应用或 manifest、测试集 | 调用 lab 的 `scripts/replay.py` 重放 |

## 常用流程

先看平台当前有哪些任务：

```bash
arcbench tasks -v
```

下载公开测试：

```bash
arcbench fetch --competition smoke
arcbench fetch --competition ticket-booking
```

从本地 harness lab 打包：

```bash
arcbench package \
  --lab-root ~/run/arcbench-harness-lab \
  --out dist/pi-harness-agent.zip
```

提交前做只读预检。`--dry-run` 会验证 ZIP、登录态和任务 ID，但不会创建提交：

```bash
arcbench submit \
  --package dist/pi-harness-agent.zip \
  --competition smoke \
  --task smoke--counter \
  --dry-run
```

真实提交并等待结果：

```bash
arcbench submit \
  --package dist/pi-harness-agent.zip \
  --competition smoke \
  --task smoke--counter \
  --name pi-harness-v1 \
  --model deepseek-v4-flash
```

只启动、不在当前进程等待：

```bash
arcbench submit ... --no-wait
arcbench status --run-id <run-id> --wait
```

把一次已有运行补记为本地结果：

```bash
arcbench status \
  --run-id <run-id> \
  --competition smoke \
  --task smoke--counter \
  --record
```

拉取一次运行的日志并提取探测需求：

```bash
arcbench logs --run-id <run-id> --out runs/official-requirements
```

## 提交安全边界

ARC-Bench 当前对同一竞赛只允许最新一次 submission 被运行。新提交可能让旧的待运行 submission 失效，因此不要把 `submit` 放进高频循环。

CLI 内置三道本地保护：

- `--dry-run` 不创建平台状态；
- JSON 记录数超过 `ARC_BENCH_MAX_SUBMISSIONS` 后拒绝继续提交；
- 两次真实提交之间执行 `ARC_BENCH_MIN_INTERVAL_SECONDS` 节流。

这些保护只作用于本机，不能协调队友从浏览器或另一台机器提交。多人共用账号时，仍需要一个外部约定或真正的共享排队器。

CLI 只做比赛允许的 Harness 提交和结果读取，不修改测试、计量器、系统时间或评分逻辑。

平台创建 run 和启动 run 是两个调用：`POST /api/runs` 先创建 `PENDING` 记录，
`POST /api/runs/{run_id}/start` 才会进入调度队列。CLI 会把两步连起来，
避免出现“提交成功但永远 PENDING”的假状态。

## 环境变量

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `ARC_BENCH_WEB_BASE_URL` | `https://arc-bench.com` | Web/API 基础地址 |
| `ARC_BENCH_API_BASE_URL` | `https://api.arc-bench.com/v1` | 模型网关基础地址 |
| `ARC_BENCH_SESSION_COOKIE` | 空 | 登录 Cookie |
| `ARC_BENCH_API_KEY` | 空 | 模型网关 key |
| `ARC_BENCH_MODEL` | `deepseek-v4-flash` | 提交时记录的模型名 |
| `ARC_BENCH_MAX_SUBMISSIONS` | `4` | 本机记录目录内的提交上限 |
| `ARC_BENCH_MIN_INTERVAL_SECONDS` | `30` | 两次真实提交的最小间隔 |
| `ARC_BENCH_HTTP_TIMEOUT_SECONDS` | `60` | HTTP 超时 |
| `ARC_BENCH_RECORD_DIR` | `./.arcbench/runs/submissions` | 结果 JSON 目录 |
| `ARCBENCH_LAB_ROOT` | 自动探测 | `package`/`replay` 使用的 lab 路径 |
| `ARCBENCH_ENV_FILE` | 未设置 | 指定 `.env` 文件 |
| `ARCBENCH_DEBUG` | 未设置 | 设为 `1` 时保留未预期异常的 traceback |

## 结果记录

每次 `submit` 完成后写一份脱敏 JSON：

```text
.arcbench/runs/submissions/<UTC>-<competition>-<task>.json
```

记录包含：

```text
submission_id, run_id, package sha256, model,
status, passed, failed, pass_rate, tokens,
duration_seconds, run_url
```

`sanitize()` 会在落盘前把 key、token、cookie、secret、password 等字段替换为 `***`。

## 开发与测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m arcbench_cli --help
```

网络测试不属于默认单元测试；默认套件不创建 ARC-Bench 提交。

## 与 `arcbench-harness-lab` 的关系

- 本仓负责平台边界：任务发现、测试包拉取、提交、轮询、记录和重放入口。
- `arcbench-harness-lab` 负责 Harness、本地 Runner、进化搜索和实验数据。
- 官方测试数据不进入本仓；`fetch` 会在使用时下载到被忽略的 `testsets/`。
- 本地 Runner 才是进化过程中的主 fitness 信号；官方平台只做低频确认。
