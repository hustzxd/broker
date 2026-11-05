# Broker

English | [简体中文](README.zh-CN.md)

Broker provides a small execution broker for agents. An `Executor`
runs on a specific server, in a specific working directory and Python
environment. The agent connects to the `Execution Broker` through MCP and
selects an executor for command execution.

```text
Codex ── MCP / Streamable HTTP ──→ Central Broker
                                      ↑
                                      │ WebSocket
                                      ├── Server A: executor
                                      ├── Server B: executor
                                      └── Server C: multiple executors
```

Run one central broker and connect all executors to it. Starting or stopping a
executor does not start or stop the broker; the broker keeps running even when
no executors are connected.

## Install

Install directly from GitHub in the Python environment used to run the broker
or an executor (Git must be installed):

```bash
python -m pip install git+https://github.com/hustzxd/broker.git
```

For local development, run the following from the repository root:

```bash
python -m pip install -e .
```

This installs the `broker` command and the MCP 2.x, WebSocket and YAML
dependencies.

This project runs independently and does not require LocalEnv. Python 3.10 or
newer is required. You can also run the CLI with `python -m broker`.

## Configuration

The default configuration file is:

```text
~/.broker/config.yaml
```

Create it with the broker and executor settings:

```yaml
broker:
  mcp_host: 0.0.0.0
  mcp_port: 8273
  executor_host: 0.0.0.0
  executor_port: 8272

executor:
  broker_url: ws://broker-host:8272
```

If the file does not exist, `broker` and `broker executor` create
it with local default values. An existing file is never overwritten.

The `broker` section controls the MCP HTTP service and the internal WebSocket
service. The `executor` section configures the central broker connection.

For a multi-server deployment with the central broker at `192.0.2.10`, use:

```yaml
broker:
  mcp_host: 0.0.0.0
  mcp_port: 8273
  executor_host: 0.0.0.0
  executor_port: 8272
executor:
  broker_url: ws://192.0.2.10:8272
```

`broker.mcp_host` and `broker.mcp_port` control the MCP HTTP listener;
`broker.executor_host` and `broker.executor_port` control the Executor WebSocket
listener. `0.0.0.0` listens on all interfaces of the broker machine.
`executor.broker_url` is the reachable central broker address used by executors.
The `broker` section is used when running `broker`; the `executor` section is
used when running `broker executor`, so the same configuration can be shared
across servers. Start `broker` only on the central server.

Command-line arguments override values from the configuration file. Another
configuration file can be selected with `--config` (before `executor` when
starting an executor):

```bash
broker --config /path/to/config.yaml
```

## Start The Broker

On the broker server:

```bash
broker
```

To override the configuration from the command line:

```bash
broker \
  --mcp-host 0.0.0.0 \
  --mcp-port 8273 \
  --executor-host 0.0.0.0 \
  --executor-port 8272
```

The MCP URL is:

```text
http://<broker-host>:8273/mcp
```

Configure this URL in the agent or MCP client.

## Connect Codex

Start the broker separately, then run the following command on the machine
where Codex runs to register the MCP server:

```bash
codex mcp add broker --url http://127.0.0.1:8273/mcp
```

For a broker on another machine, replace `broker-host` with its IP address or
domain name (use the configured MCP port if it differs from `8273`):

```bash
codex mcp add broker --url http://broker-host:8273/mcp
```

The remote broker must listen on a reachable interface, for example with
`broker --mcp-host 0.0.0.0`. Use the MCP HTTP port (`8273`), rather than the Executor
WebSocket port (`8272`).

The command saves the server in Codex's global `~/.codex/config.toml`:

```toml
[mcp_servers.broker]
url = "http://127.0.0.1:8273/mcp"
```

Check the registration with:

```bash
codex mcp get broker
```

Restart Codex or reconnect the MCP server to load the configuration. Registering
the URL does not start the broker; keep the broker running separately. Start a
executor as described below before using command execution tools.

See the [official Codex MCP documentation](https://developers.openai.com/codex/mcp/).

## Start A Executor

Start the executor in the target working directory and Python environment. The
executor inherits both when it starts. By default, the command asks for a
executor name. Names must be non-empty and are supplied at startup, either
interactively or with `--executor-id`; they are not read from the configuration:

```bash
cd /data/project-a
conda activate train
broker executor
```

If the name is already used by another online executor, the command asks for a
different name and retries registration without restarting.

Commands run using the shell from the Executor environment's `$SHELL` setting,
falling back to `/bin/sh` when it is not available. To use zsh, start the
Executor from a zsh environment or set `SHELL=/bin/zsh`.

The executor can also be started with the Python executable from the target
environment:

```bash
cd /data/project-a
/opt/conda/envs/train/bin/broker executor
```

To override the configured executor values:

```bash
broker executor \
  --broker-url ws://broker-host:8272 \
  --executor-id gpu01-project-a
```

The broker port can be overridden without rewriting the URL (the host and
scheme come from `--broker-url` or `config.yaml`):

```bash
broker executor --port 9000 --executor-id gpu01-project-a
```

## Use Broker In Codex

After starting the central broker and configuring its MCP connection in Codex,
start an executor in the directory you want to inspect:

```bash
cd /data/project-a
broker executor --executor-id demo
```

Keep the broker and executor running, then enter this prompt in Codex:

```text
Use broker to run ls on the demo executor and return the result.
```

Codex can check that the executor is online with `list_executors`, then call
`execute`:

```text
execute(executor_id="demo", command="ls")
```

The command runs in the directory where the demo executor was started. Codex
uses the actual result to report the directory contents and exit code.

Codex calls broker and displays the `ls` result:

<img src="images/demo_codex.png" alt="Codex executing ls through broker and returning the result" width="800">

The executor terminal shows the command, output and exit code:

![Executor terminal showing the ls execution log](images/demo_terminal.png)

You can also ask for online executors or a more detailed directory listing:

```text
Use broker to list online executors, including their names, working directories and Python paths.
```

```text
Use broker to run pwd and ls -la on the demo executor. Return the working directory, file listing and exit code.
```

When multiple executors are connected, specify the executor name in your prompt
to identify where the command should run.

## MCP Tools

The broker exposes these tools to the agent:

### `list_executors`

Lists currently connected executors, including their executor ID,
working directory and Python executable.

### `execute`

Runs a one-shot shell command and waits for its result. The default timeout is
60 seconds:

```text
execute(
  executor_id="gpu01-project-a",
  command="bash train.sh"
)
```

Completed commands return their output and return code directly. If the
command exceeds the timeout, the result contains a `task_id` with status
`running`; use `get_task` to query it:

```text
get_task(task_id="<task-id>")
```

The timeout can be changed per call:

```text
execute(
  executor_id="gpu01-project-a",
  command="bash long_test.sh",
  timeout=300
)
```

### `get_task`

Returns the task status, stdout, stderr and return code for a command that has
not completed before its `execute` timeout:

```text
get_task(task_id="<task-id>")
```

## Long-Running Services

Use the service tools for processes that are expected to keep running, such as
vLLM or TensorBoard. Services have their own lifecycle and are not treated as
completed shell commands.

Start a service:

```text
start_service(
  executor_id="gpu01-project-a",
  name="vllm-qwen",
  command="python -m vllm.entrypoints.openai.api_server --model /models/Qwen --port 8000",
  ready_check={"type": "tcp", "port": 8000},
  startup_timeout=180
)
```

The result contains a `service_id`. Use the following tools to manage it:

```text
get_service(service_id="<service-id>")
get_service_logs(service_id="<service-id>", tail=200)
list_services(executor_id="gpu01-project-a")
stop_service(service_id="<service-id>")
```

Supported service states are `starting`, `ready`, `stopping`, `stopped` and
`failed`. Readiness checks currently support TCP port availability and are
performed inside the Executor machine.

## Network Requirements

The broker server must accept:

- MCP HTTP traffic on `mcp_port`, default `8273`.
- Executor WebSocket traffic on `executor_port`, default `8272`.

The executor server must be able to connect to the broker's Executor WebSocket
address.

The current implementation does not provide application-level authentication.
Do not expose the broker directly to an untrusted network. Use a private
network, firewall rules or an authenticated reverse proxy when remote access
is required.

## Development Checks

Run the basic syntax and import checks from the repository root:

```bash
PYTHONPATH=src python -m py_compile src/broker/*.py
```
