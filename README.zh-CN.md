# Broker

[English](README.md) | 简体中文

通过 MCP 在远程机器执行 Shell 命令、管理长期服务。每个 executor 使用启动时的工作目录和 Python 环境。

```text
Agent ── MCP HTTP ──→ Broker ←── WebSocket ── Executors
```

## 安装

需要 Python 3.10+ 和 Git。在 broker 和 executor 所在机器安装：

```bash
python -m pip install --upgrade git+https://github.com/hustzxd/broker.git
# 本地开发：
python -m pip install -e .
broker --version
# broker v0.1.38
```

## 启动中心 Broker

```bash
broker                     # TUI 总览，按 executor 分页
broker --output-mode raw   # 普通日志，适合后台运行
```

首次运行创建 `~/.broker/config.yaml`，并随机生成 `broker.key`。默认目录权限为 `700`，配置文件为 `600`，已有 Key 保留。

默认监听 `127.0.0.1`：MCP 地址为 `http://127.0.0.1:8273/mcp`，executor WebSocket 地址为 `ws://127.0.0.1:8272`。需要远程连接时开放监听接口：

```bash
broker --mcp-host 0.0.0.0 --executor-host 0.0.0.0
```

Web UI 随 broker 一起启动，共用 HTTP 端口。中心 TUI 提供完整的可点击链接；打开 `http://127.0.0.1:8273/ui/`，输入 `broker.key` 即可按 executor 查看 Tasks/Services、实时进度日志，展开/折叠、暂停跟随，并取消任务或停止服务。Key 保存在当前标签页的会话存储中，刷新后自动连接；认证失效时自动清除，关闭标签页结束会话。TUI 和 raw 模式均提供 Web UI，broker 退出时一起关闭；远程访问同样需要 HTTPS 或加密隧道。

Web 页面会在标签页会话中保留 executor/分类选择、卡片展开、跟随模式、搜索筛选和滚动位置。记录按最新提交排序，可按状态筛选或搜索名称、命令和 ID；日志支持关键词筛选匹配行、复制与下载 `.txt`，复制/下载包含已加载的完整保留文本，不仅是匹配行。暂停跟随可固定当前快照。后台标签页暂停轮询，连接失败时显示最后更新时间和最长 30 秒的退避重试倒计时。离线历史可确认后清除，在线和活跃记录受保护；服务端渲染日志使用限量缓存，不新增日志文件。

顶部主题按钮可切换深色/浅色，浏览器会记住主题偏好，不影响登录状态。

Service 结束或失败时提供页面内提醒，顶部、executor 侧栏和 Services 页签显示未读数。点击提醒可定位并展开服务，主动展开详情标为已读，也可点击“标为已读”或“全部已读”；刷新/切换视图时自动恢复展开状态不会清除未读。首次加载不提醒已有历史；未读状态在当前标签页会话中保存，刷新不重复弹提醒，后台标签页返回后同步。Task 不提醒，不申请桌面通知权限或播放声音。

## 启动 Executor

在目标目录和 Python 环境中执行：

```bash
cd /data/project-a
conda activate train
broker executor --executor-id demo
```

连接远程 broker 时，在 executor 机器的 `~/.broker/config.yaml` 中设置：

```yaml
executor:
  broker_url: ws://broker-host:8272
  key: "<中心 broker.key>"
```

地址也可通过 `--broker-url ws://broker-host:8272` 覆盖。Key 读取顺序为 `BROKER_KEY`、`executor.key`、本机 `broker.key`；远程机器自动生成的 Key 不会与中心 Key 自动匹配。

Broker 和 executor 默认使用 TUI。Executor 另支持 `--output-mode raw` 和 `--output-mode prefixed`，重定向输出时使用 raw。命令行优先于 YAML；自定义配置参数 `--config /path/to/config.yaml` 放在 `executor` 子命令之前。

## 接入 Codex

在 `~/.codex/config.toml` 中添加，填写中心 broker 的 Key：

```toml
[mcp_servers.broker]
url = "http://127.0.0.1:8273/mcp"
http_headers = { Authorization = "Bearer <中心 broker.key>" }
```

将此文件权限设为 `600`。也可将 `http_headers` 换成 `bearer_token_env_var = "BROKER_KEY"`，在**启动 Codex 前**设置环境变量。该字段填写变量名，不是 Key 值。

重启 Codex 或重新连接 MCP，保持 broker 和至少一个 executor 运行，然后输入：

```text
使用 broker 在 demo executor 上运行 ls，返回结果。
```

Codex 自动携带 Key，工具参数无需包含凭证，也不需要 OAuth 登录。参见 [Codex MCP 文档](https://developers.openai.com/codex/mcp/)。

## Harness 提示词

可将以下内容复制到 Codex 等 harness 的 `AGENTS.md` 或系统提示词中：

```markdown
## 命令执行：本地工具与 Broker 的选择

根据命令是否依赖目标工作目录、Python 环境或服务器资源，选择执行方式。

### 使用 Broker

以下命令应通过 Broker 在对应 executor 中执行：
- 需要目标项目工作目录的命令，例如运行项目脚本、测试、构建、实验，以及依赖相对路径的操作。
- 需要目标 Python/Conda/虚拟环境的命令，例如 Python 推理、训练、依赖检查和安装。
- 需要目标服务器文件、GPU、端口或其他资源的命令。
- 需要在目标环境启动和管理的长期服务。

执行前：
1. 调用 `list_executors`，根据用户指定的名称，以及返回的 `cwd`、`python` 等信息选择 executor。
2. 不要猜测 executor ID，也不要假设 executor 的目录或 Python 环境与当前会话一致。
3. 必要时通过 Broker 执行 `pwd` 和 `python -c 'import sys; print(sys.executable)'` 确认环境。
4. 找不到匹配 executor 时，明确说明缺少哪个环境；不要直接在本地执行依赖该环境的命令。

执行时：
- 普通命令使用 `execute`；等待超时不代表命令失败，也不会终止命令。使用返回的 `task_id` 调用 `get_task` 继续查询，不要重复提交。
- 增量查询时，将上次返回的 `next_cursor` 作为下一次的 `cursor`。
- 需要取消命令时使用 `cancel_task`。
- 长期服务使用 `start_service` 和相应服务管理工具。
- 任务状态为 `lost` 时，先检查执行结果和副作用，再决定是否重试。

### 使用本地工具

不依赖目标环境的操作直接使用 harness 自带工具，无需经过 Broker，例如：
- 编辑当前工作区文件、应用补丁。
- 阅读和搜索当前工作区中已可访问的源码、文档。
- 通用文本处理、格式整理，以及不依赖目标环境的简单计算。

不要为了使用 Broker 而把所有命令都转发过去。判断依据是命令的实际环境依赖，而不是命令名称：例如，读取本地 README 可以直接进行，读取远程服务器日志需要 Broker；通用 Python 计算可以本地执行，项目 Python 脚本应在匹配的 executor 中执行。

用户明确指定执行位置或工具时，优先遵循用户要求。报告结果时，说明关键命令使用的 executor，并以实际输出和退出码为依据。
```

## 常用工具

| 工具 | 用途 |
|---|---|
| `list_executors` | 在线 executor、目录、Python 路径和版本 |
| `execute(executor_id, command, timeout=60)` | 提交命令并等待结果 |
| `list_tasks(executor_id=None, include_completed=False)` | 任务元数据 |
| `get_task(task_id, cursor=None)` | 状态、输出、退出码及下一游标 |
| `cancel_task(task_id)` | 取消排队任务或终止任务进程组 |
| `start_service(executor_id, name, command, ...)` | 启动长期服务 |
| `list_services(executor_id=None, include_inactive=False)` | 服务元数据 |
| `get_service(service_id)` | 服务状态及失败原因 |
| `get_service_logs(service_id, tail=200, cursor=None)` | 保留的输出及下一游标 |
| `stop_service(service_id)` | 停止进程组，5 秒后仍未退出则强制终止 |

同一 executor 的普通命令串行执行。`execute` 超时只结束等待，不终止命令；用 `get_task` 查询或 `cancel_task` 取消。将上次的 `next_cursor` 作为 `cursor` 传入即可增量读取，`truncated` 表示有输出被省略。服务日志返回对象包含 `logs`、`truncated` 和 `next_cursor`。

带 TCP 就绪检查的服务示例：

```text
start_service(executor_id="demo", name="tensorboard",
  command="tensorboard --logdir ./runs --port 6006",
  ready_check={"type": "tcp", "port": 6006}, startup_timeout=120)
```

## 界面与运行行为

- 中心 TUI 和 Web UI 均将在线 executor 排在前面，离线项标注 OFFLINE 并排在后面，组内按名称排序。Broker 按 executor 分页，内部再分 Tasks/Services；executor 也分别展示 Tasks/Services。
- 卡片默认折叠，任务标题预览最多 60 个命令字符，超长以 `…` 截断，展开可查看完整命令。点击标题展开，F2/F3 展开/折叠当前视图。暂停跟随后可滚动和选择日志，Ctrl+C 复制；Ctrl+Q 退出并关闭连接或清理本地进程。
- TUI 日志识别回车及常见光标移动/清行指令，tqdm 进度条可原位刷新；broker 原始输出不变，不提供完整终端模拟。
- Executor 断线后按退避重连。Broker 检测到断线后，未完成任务变为 `lost`，不会自动重放；服务在断线后停止，不跨连接恢复。
- 任务和服务详情展示本地时区的生命周期时间戳（提交、启动、就绪、结束），同时保留耗时显示。
- 历史仅存内存。Broker 默认每个任务/服务保留 1 MiB 输出、100 个已完成任务、100 个终态服务，每个 executor 最多 32 个未完成命令。旧历史会淘汰，超额提交返回 `queue_full`。

可在 `config.yaml` 中调整：

```yaml
broker:
  output_mode: tui
  max_log_bytes: 1048576
  max_completed_tasks: 100
  max_completed_services: 100
  max_pending_tasks: 32
```

## 诊断与安全

```bash
broker doctor                              # 检查连接、Key 和 MCP 工具发现
broker doctor --url https://broker.example.com/mcp
```

两个入口均需共享 Key，持有者拥有全部权限，命令使用 executor 用户的操作系统权限。Key 认证**不加密传输**；远程访问请通过 TLS 代理使用 HTTPS/WSS，或使用加密 SSH/VPN 隧道，并阻止绕过代理访问后端端口。

轮换凭证时修改 `broker.key`，重启 broker，并同步更新客户端/executor。环境变量 `BROKER_KEY` 优先于配置。版本不一致时会提示升级，请在两端环境升级并重启。

## 开发

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Web DOM 回归（需要 Node.js 和 jsdom）：

```bash
npm install --prefix /tmp/broker-web-tests --no-save jsdom
NODE_PATH=/tmp/broker-web-tests/node_modules node tests/web_ui.cjs
```

每项完成的变更递增 `src/broker/version.py` 的补丁版本，同步两份 README 的版本示例，并核对 CLI 与包元数据版本一致。
