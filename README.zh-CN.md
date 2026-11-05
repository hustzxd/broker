# Broker

[English](README.md) | 简体中文

Broker 为 AI Agent 提供命令执行服务。每个执行器（Executor）运行在特定服务器、工作目录和 Python 环境中。Agent 通过 MCP 连接中心 Broker，选择 executor 执行命令。

```text
Codex ── MCP / Streamable HTTP ──→ 中心 Broker
                                      ↑
                                      │ WebSocket
                                      ├── 服务器 A 的 executor
                                      ├── 服务器 B 的 executor
                                      └── 服务器 C 的多个 executor
```

只需运行一个中心 broker，所有 executor 都连接到它。启动或关闭 executor 不会启动或关闭 broker；即使所有 executor 都断开，broker 仍继续运行。

## 安装

在运行 broker 或 executor 的 Python 环境中直接从 GitHub 安装，需要预先安装 Git：

```bash
python -m pip install git+https://github.com/hustzxd/broker.git
```

本地开发时，在仓库根目录执行：

```bash
python -m pip install -e .
```

安装后会提供 `broker` 命令，并安装 MCP 2.x、WebSocket 和 YAML 依赖。

本项目独立运行，无需安装 LocalEnv。需要 Python 3.10 或更新版本，也可以使用 `python -m broker` 运行命令行工具。

查看安装版本：

```bash
broker --version
# broker v0.1.1
```

后续每次修改后，递增 `src/broker/version.py` 中的补丁版本号，例如从 `0.1.1` 升到 `0.1.2`。命令行和安装包元数据使用同一份版本定义。

## 配置

默认配置文件路径：

```text
~/.broker/config.yaml
```

配置示例：

```yaml
broker:
  mcp_host: 0.0.0.0
  mcp_port: 8273
  executor_host: 0.0.0.0
  executor_port: 8272

executor:
  broker_url: ws://broker-host:8272
```

如果配置文件不存在，运行 `broker` 或 `broker executor` 时会自动创建，默认监听本机回环地址 `127.0.0.1`。已有配置文件不会被覆盖。上面的示例使用 `0.0.0.0`，适合需要接受其他机器连接的中心服务。

`broker` 部分配置 MCP HTTP 服务和 Executor WebSocket 服务；`executor` 部分配置中心 broker 的连接地址。

如果中心 broker 位于 `192.0.2.10`，多服务器部署可以使用：

```yaml
broker:
  mcp_host: 0.0.0.0
  mcp_port: 8273
  executor_host: 0.0.0.0
  executor_port: 8272
executor:
  broker_url: ws://192.0.2.10:8272
```

| 配置项 | 含义 |
|---|---|
| `broker.mcp_host` / `mcp_port` | MCP HTTP 的监听地址和端口，供 Codex 等 MCP 客户端连接 |
| `broker.executor_host` / `executor_port` | Executor WebSocket 的监听地址和端口，供 executor 连接 |
| `executor.broker_url` | executor 使用的中心 broker 地址 |

`0.0.0.0` 表示监听 broker 所在机器的所有 IPv4 网络接口。executor 应连接中心服务器可访问的 IP 或域名。

执行 `broker` 时读取 `broker` 部分，执行 `broker executor` 时读取 `executor` 部分，因此同一份配置可以分发到多台服务器。只在中心服务器启动 `broker`，其他服务器启动 executor。

命令行参数优先于配置文件。可以使用 `--config` 指定其他配置文件，启动 executor 时需将其放在 `executor` 子命令之前：

```bash
broker --config /path/to/config.yaml
```

## 启动中心 Broker

在中心服务器上执行：

```bash
broker
```

也可以通过命令行覆盖配置：

```bash
broker \
  --mcp-host 0.0.0.0 \
  --mcp-port 8273 \
  --executor-host 0.0.0.0 \
  --executor-port 8272
```

MCP 服务地址为：

```text
http://<broker-host>:8273/mcp
```

将该地址配置到 Agent 或 MCP 客户端中。

## 接入 Codex

先单独启动 broker，再在运行 Codex 的机器上注册 MCP 服务。

如果 Codex 和 broker 在同一台机器：

```bash
codex mcp add broker --url http://127.0.0.1:8273/mcp
```

如果 broker 在另一台机器，将 `broker-host` 替换为中心服务器的 IP 或域名。如果修改了 MCP 端口，也需要替换 `8273`：

```bash
codex mcp add broker --url http://broker-host:8273/mcp
```

远程 broker 需要监听其他机器可访问的网络接口，例如使用 `broker --mcp-host 0.0.0.0`。Codex 连接的是 MCP HTTP 端口 `8273`，executor 连接的是 WebSocket 端口 `8272`。

该命令会将服务写入 Codex 的全局配置 `~/.codex/config.toml`：

```toml
[mcp_servers.broker]
url = "http://127.0.0.1:8273/mcp"
```

检查配置：

```bash
codex mcp get broker
```

重启 Codex 或重新连接 MCP 服务以加载配置。注册地址不会启动 broker，仍需单独保持 broker 运行。使用命令执行工具前，还需要按下文启动至少一个 executor。

参考 [Codex 官方 MCP 文档](https://developers.openai.com/codex/mcp/)。

## 启动 Executor

在目标工作目录和 Python 环境中启动 executor，它会继承启动时的目录和环境。默认会提示输入 executor 名称，名称不能为空。名称由用户在启动时输入，或通过 `--executor-id` 指定，不从配置文件读取：

```bash
cd /data/project-a
conda activate train
broker executor
```

如果名称已被其他在线 executor 占用，会提示输入其他名称，并在同一次连接中重新注册，无需重启。

命令使用 executor 环境中的 `$SHELL`，不可用时回退到 `/bin/sh`。需要使用 zsh 时，可以从对应环境启动 executor，或设置 `SHELL=/bin/zsh`。

也可以直接使用目标 Python 环境中安装的命令启动：

```bash
cd /data/project-a
/opt/conda/envs/train/bin/broker executor
```

通过命令行覆盖 executor 配置：

```bash
broker executor \
  --broker-url ws://broker-host:8272 \
  --executor-id gpu01-project-a
```

只修改 broker 的 WebSocket 端口时，无需重写整个 URL；主机和协议仍取自 `--broker-url` 或配置文件：

```bash
broker executor --port 9000 --executor-id gpu01-project-a
```

如果无法连接中心 broker，会显示目标地址、启动中心服务的提示以及连接失败原因。请检查中心服务是否运行，以及地址、监听接口和端口是否正确。

## 在 Codex 中使用

启动中心 broker、完成 Codex MCP 配置后，在要查看的目录中启动一个 executor：

```bash
cd /data/project-a
broker executor --executor-id demo
```

保持 broker 和 executor 运行，在 Codex 对话框中输入：

```text
使用 broker 在 demo executor 上运行 ls 指令并返回结果。
```

Codex 可以通过 `list_executors` 确认执行器在线，再调用 `execute`：

```text
execute(executor_id="demo", command="ls")
```

命令会在 demo executor 启动时的工作目录中执行，Codex 根据实际返回结果展示目录内容和退出码。

Codex 调用 broker 并展示 `ls` 结果：

<img src="images/demo_codex.png" alt="Codex 通过 broker 执行 ls 并返回结果" width="800">

Executor 终端同步显示命令、输出和退出码：

![Executor 终端中的 ls 执行日志](images/demo_terminal.png)

也可以直接请求查看在线执行器，或指定更详细的目录输出：

```text
使用 broker 列出当前在线的 executor，显示名称、工作目录和 Python 路径。
```

```text
使用 broker 在 demo executor 上运行 pwd 和 ls -la，返回工作目录、文件列表和退出码。
```

如果有多个 executor，请在提示词中指定名称，以明确命令的执行位置。

## MCP 工具

Broker 向 Agent 提供以下工具。

### `list_executors`

列出当前在线的 executor，包括 executor ID、工作目录和 Python 可执行文件路径。

### `execute`

执行一次性 Shell 命令并等待结果，默认等待 60 秒：

```text
execute(
  executor_id="gpu01-project-a",
  command="bash train.sh"
)
```

命令完成后直接返回输出和退出码。如果超过等待时间，返回的状态为 `running` 并包含 `task_id`，可通过 `get_task` 查询。超时返回不会终止命令：

```text
get_task(task_id="<task-id>")
```

每次调用可以单独设置等待时间：

```text
execute(
  executor_id="gpu01-project-a",
  command="bash long_test.sh",
  timeout=300
)
```

### `get_task`

查询已提交任务的状态、标准输出、标准错误和退出码，适用于未在 `execute` 等待时间内完成的命令：

```text
get_task(task_id="<task-id>")
```

## 长期运行的服务

对于 vLLM、TensorBoard 等需要持续运行的进程，使用服务管理工具。服务拥有独立的生命周期。

启动服务：

```text
start_service(
  executor_id="gpu01-project-a",
  name="vllm-qwen",
  command="python -m vllm.entrypoints.openai.api_server --model /models/Qwen --port 8000",
  ready_check={"type": "tcp", "port": 8000},
  startup_timeout=180
)
```

返回结果包含 `service_id`，可通过以下工具查询和管理：

```text
get_service(service_id="<service-id>")
get_service_logs(service_id="<service-id>", tail=200)
list_services(executor_id="gpu01-project-a")
stop_service(service_id="<service-id>")
```

服务状态包括 `starting`、`ready`、`stopping`、`stopped` 和 `failed`。就绪检查目前支持 TCP 端口检查，在 executor 所在机器上执行。

## 网络要求

中心服务器需要允许以下连接：

- MCP HTTP：`mcp_port`，默认 `8273`。
- Executor WebSocket：`executor_port`，默认 `8272`。

executor 所在服务器必须能够访问中心 broker 的 Executor WebSocket 地址。

当前实现没有应用层身份认证。请通过私有网络、防火墙规则或带身份认证的反向代理提供远程访问，避免直接暴露到不可信网络。

## 开发检查

在仓库根目录执行基本语法检查：

```bash
PYTHONPATH=src python -m py_compile src/broker/*.py
```
