# Broker

English | [简体中文](README.zh-CN.md)

Run shell commands and manage services on remote machines through MCP. Each executor uses the working directory and Python environment where it starts.

```text
Agent ── MCP HTTP ──→ Broker ←── WebSocket ── Executors
```

## Install

Requires Python 3.10+ and Git. Install on the broker and executor machines:

```bash
python -m pip install --upgrade git+https://github.com/hustzxd/broker.git
# Local development:
python -m pip install -e .
broker --version
# broker v0.1.35
```

## Start the broker

```bash
broker                     # TUI dashboard, grouped by executor
broker --output-mode raw   # Plain logs for background operation
```

The first run creates `~/.broker/config.yaml` with a random `broker.key`. The default directory has mode `700`, and the config file has mode `600`. Existing keys are preserved.

Defaults listen on `127.0.0.1`: MCP at `http://127.0.0.1:8273/mcp`, executor WebSocket at `ws://127.0.0.1:8272`. For remote connections, set reachable listeners:

```bash
broker --mcp-host 0.0.0.0 --executor-host 0.0.0.0
```

The Web UI starts with the broker on the same HTTP port: open
`http://127.0.0.1:8273/ui/` (also shown as a clickable link in the broker TUI) and enter `broker.key`. It groups Tasks/Services by
executor and supports live progress logs, collapse/expand, pause-follow and
cancel/stop controls. The key is kept in the tab's session storage so refreshing
reconnects automatically. An authentication failure clears it;
closing the tab ends the session. Both TUI and raw server modes include the Web UI; closing the broker stops
it too. Remote access needs HTTPS or an encrypted tunnel, just like MCP.

Web navigation remembers the executor/category, expanded cards, follow mode,
search filters and scroll positions within the tab session. Records are newest
first and can be filtered by status or searched by name, command and ID. Expanded
logs support keyword line filtering, copying and `.txt` download of the loaded
retained text (not only matching lines). Pause follow for a stable snapshot.
Polling pauses in background tabs; connection failures show the last update and
retry countdown with backoff up to 30 seconds. Offline history can be cleared
after confirmation; online or active records are protected. Server-side rendered
logs use a bounded cache. No server log files are created.

Use the header theme switch for dark/light mode; the browser remembers this preference independently of login.

Service completion/failure produces in-page reminders and unread counts in the
header, executor sidebar and Services tab. Click a reminder to open its service;
explicitly expanding details marks it read, or use “标为已读” / “全部已读”.
Automatically restoring an open card after refresh/navigation never clears unread reminders.
Initial history is quiet; unread state survives refresh within the tab session.
Background tabs catch up on return. Tasks never trigger reminders; no desktop
notification permission or sound is used.

## Start an executor

In the target directory and Python environment:

```bash
cd /data/project-a
conda activate train
broker executor --executor-id demo
```

For a remote broker, configure the executor machine's `~/.broker/config.yaml`:

```yaml
executor:
  broker_url: ws://broker-host:8272
  key: "<central broker.key>"
```

Or override the address with `--broker-url ws://broker-host:8272`. Key lookup order is `BROKER_KEY`, `executor.key`, then local `broker.key`; a remote machine's generated key does not match the central key automatically.

Both broker and executor default to TUI. Executors also support `--output-mode raw` and `--output-mode prefixed`. Use raw mode for redirected output. CLI options override YAML; place `--config /path/to/config.yaml` before the `executor` subcommand.

## Connect Codex

Add this to `~/.codex/config.toml`, using the central broker's key:

```toml
[mcp_servers.broker]
url = "http://127.0.0.1:8273/mcp"
http_headers = { Authorization = "Bearer <central broker.key>" }
```

Restrict this file to mode `600`. Alternatively, replace `http_headers` with `bearer_token_env_var = "BROKER_KEY"` and set that environment variable **before starting Codex**. The field takes the variable name, not the key value.

Restart Codex or reconnect MCP. Keep the broker and at least one executor running, then ask:

```text
Use broker to run ls on the demo executor and return the result.
```

Codex sends the key automatically; tool arguments do not include it. No OAuth login is needed. See [Codex MCP documentation](https://developers.openai.com/codex/mcp/).

## Harness prompt

Copy the following into `AGENTS.md` or the system prompt of Codex or another harness:

```markdown
## Choosing between local tools and Broker

Choose the execution tool based on the command's working-directory, Python-environment and server-resource dependencies.

### Use Broker

Run these commands through the matching executor:
- Commands requiring the target project directory: project scripts, tests, builds, experiments and relative-path operations.
- Commands requiring the target Python/Conda/virtual environment: inference, training, dependency checks and installation.
- Commands requiring files, GPUs, ports or other resources on the target server.
- Long-running services that must run in the target environment.

Before execution:
1. Call `list_executors` and choose by the user's requested name and the returned `cwd` and `python` metadata.
2. Never guess executor IDs or assume an executor shares this session's directory or Python environment.
3. When necessary, verify the environment through Broker using `pwd` and `python -c 'import sys; print(sys.executable)'`.
4. If no executor matches, state which environment is unavailable. Do not silently run environment-dependent commands locally.

During execution:
- Use `execute` for commands. A wait timeout neither fails nor terminates the command. Poll the returned `task_id` with `get_task`; do not resubmit it.
- For incremental reads, pass the previous `next_cursor` as `cursor`.
- Use `cancel_task` to cancel commands.
- Use `start_service` and the service management tools for long-running services.
- If a task becomes `lost`, check its outcome and side effects before deciding whether to retry.

### Use local tools

Use the harness's built-in tools for operations that do not depend on the target environment:
- Editing workspace files and applying patches.
- Reading and searching source code or documents already accessible in the current workspace.
- General text processing, formatting and simple environment-independent calculations.

Do not forward every command through Broker. Decide by actual environment dependencies, not command names: reading a local README can use local tools, while reading remote server logs requires Broker; generic Python calculations can run locally, while project Python scripts belong in the matching executor.

Follow explicit user instructions about execution location and tools. Report the executor used for key commands and base conclusions on actual output and exit codes.
```

## Tools

| Tool | Purpose |
|---|---|
| `list_executors` | Online executors, directories, Python paths and versions |
| `execute(executor_id, command, timeout=60)` | Submit a command and wait up to the timeout |
| `list_tasks(executor_id=None, include_completed=False)` | Task metadata |
| `get_task(task_id, cursor=None)` | Status, output, exit code and next cursor |
| `cancel_task(task_id)` | Cancel a queued command or kill its process group |
| `start_service(executor_id, name, command, ...)` | Start a long-running service |
| `list_services(executor_id=None, include_inactive=False)` | Service metadata |
| `get_service(service_id)` | Service status and failure reason |
| `get_service_logs(service_id, tail=200, cursor=None)` | Retained output and next cursor |
| `stop_service(service_id)` | Stop the process group; force termination after 5 seconds |

Commands run serially per executor. An `execute` timeout ends only the wait; use `get_task` to poll or `cancel_task` to stop it. Pass the previous `next_cursor` as `cursor` to read only new output; `truncated` reports omitted output. Service log responses contain `logs`, `truncated` and `next_cursor`.

Example service with a TCP readiness check:

```text
start_service(executor_id="demo", name="tensorboard",
  command="tensorboard --logdir ./runs --port 6006",
  ready_check={"type": "tcp", "port": 6006}, startup_timeout=120)
```

## TUI and runtime behavior

- Broker TUI and Web UI list online executors first, followed by labeled OFFLINE entries; each group is sorted by name. Broker: one tab per executor, with separate Tasks/Services views. Executor: separate Tasks/Services tabs.
- Cards start collapsed; task titles preview up to 60 command characters, with `…` for longer commands. Expand to see the full command. Click a title to expand; F2/F3 expand/collapse the current view. Pause follow to scroll/select logs; Ctrl+C copies, Ctrl+Q exits and closes connections or cleans up local processes.
- TUI logs interpret carriage returns and common cursor/erase controls so tqdm progress bars update in place. Raw broker output stays unchanged; this is not a full terminal emulator.
- Executors reconnect with backoff. Unfinished tasks become `lost` when the broker detects a disconnect and are never replayed. Services stop on disconnect; they do not survive reconnects.
- Task/service details show lifecycle timestamps in the viewer's local timezone; elapsed durations remain visible.
- History is memory-only. Broker defaults: 1 MiB output per task/service, 100 completed tasks, 100 finished services, 32 unfinished commands per executor. Old history is evicted; excess commands return `queue_full`.

Optional broker settings in `config.yaml`:

```yaml
broker:
  output_mode: tui
  max_log_bytes: 1048576
  max_completed_tasks: 100
  max_completed_services: 100
  max_pending_tasks: 32
```

## Diagnostics and security

```bash
broker doctor                              # Connection, key and MCP tool discovery
broker doctor --url https://broker.example.com/mcp
```

Both entrances require the shared key; every holder has full access. Commands have the executor user's OS permissions. Key authentication does **not** encrypt traffic: use HTTPS/WSS via a TLS proxy or encrypted SSH/VPN tunnels for remote access. Prevent access to unprotected backend ports.

To rotate credentials, change `broker.key`, restart the broker and update clients/executors. `BROKER_KEY` overrides configured credentials. Version mismatches produce an upgrade warning; upgrade both environments and restart.

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Web DOM regression (requires Node.js and jsdom):

```bash
npm install --prefix /tmp/broker-web-tests --no-save jsdom
NODE_PATH=/tmp/broker-web-tests/node_modules node tests/web_ui.cjs
```

For each completed change, bump the patch version in `src/broker/version.py`, update both README version examples and verify CLI/package metadata agree.
