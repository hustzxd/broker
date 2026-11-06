"""Executor daemon."""

import asyncio
import codecs
import json
import logging
import os
import signal
import sys
import time
import re
from contextvars import ContextVar

from websockets.asyncio.client import connect, process_exception
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from .protocol import message, validate_service
from .auth import validate_key
from .version import __version__, warn_version_mismatch

logger = logging.getLogger(__name__)


def _get_shell():
    shell = os.environ.get("SHELL")
    return shell if shell and os.path.isfile(shell) else "/bin/sh"


# Console presentation. The executor's stdout is the operator's terminal, so the
# escaping is for humans only: nothing here is sent over the broker socket.
RULE_WIDTH = 72
_COLOR = os.environ.get("NO_COLOR") is None and (
    os.environ.get("FORCE_COLOR") is not None or sys.stdout.isatty()
)
_partial_output_stream = None
_partial_output_source = None
_OUTPUT_MODE = ContextVar("executor_output_mode", default="raw")


def _color(text, *codes):
    """Wrap text in ANSI codes when the console is a terminal."""
    if not _COLOR or not codes:
        return text
    return "".join(f"\033[{code}m" for code in codes) + text + "\033[0m"


def _emit(line=""):
    global _partial_output_stream, _partial_output_source
    if _partial_output_stream is not None:
        _partial_output_stream.write("\n")
        _partial_output_stream.flush()
        _partial_output_stream = None
    _partial_output_source = None
    print(line, flush=True)


def _write_output(text, stream):
    global _partial_output_stream, _partial_output_source
    stream.write(text)
    stream.flush()
    _partial_output_stream = None if text.endswith("\n") else stream
    _partial_output_source = None


class _OutputWriter:
    """Stream local labels without buffering partial lines or altering wire data."""

    def __init__(self, label, stream_name):
        self.label = "".join(char if char.isprintable() else "?" for char in label)
        self.stream_name = stream_name
        self.after_cr = False

    def write(self, text):
        global _partial_output_source
        stream = sys.stdout if self.stream_name == "stdout" else sys.stderr
        if _OUTPUT_MODE.get() == "raw":
            _write_output(text, stream)
            return
        for part in re.split(r"(\r\n|\r|\n)", text):
            if not part:
                continue
            if part == "\n" and self.after_cr:
                self.after_cr = False
                continue
            self.after_cr = part.endswith("\r")
            if _partial_output_stream is not None and _partial_output_source is not self:
                _partial_output_stream.write("\n")
                _partial_output_stream.flush()
                _finish_output_line()
            if _partial_output_stream is None:
                _write_output(f"[{self.label} {self.stream_name}] ", stream)
            _write_output("\n" if part in {"\r", "\n", "\r\n"} else part, stream)
            if _partial_output_stream is not None:
                _partial_output_source = self


def _finish_output_line():
    global _partial_output_stream, _partial_output_source
    _partial_output_stream = None
    _partial_output_source = None


def _rule(label="", *codes):
    if label:
        fill = "─" * max(1, RULE_WIDTH - len(label) - 6)
        text = f"──── {label} {fill}"
    else:
        text = "─" * RULE_WIDTH
    return _color(text, *codes)


def _print_command_start(command, task_id, waited=None):
    _emit()
    waiting = f" · waited {waited:.2f}s" if waited is not None else ""
    _emit(_rule(f"{time.strftime('%H:%M:%S')} · task {task_id[:8]} RUNNING{waiting}", "2"))
    _emit(_color(f"$ {command}", "1", "36"))


def _print_command_end(returncode, elapsed, cancel_reason=None):
    if cancel_reason:
        label = f"CANCELLED · {cancel_reason} · exit {returncode}"
        color = "33"
    else:
        label = f"exit {returncode}"
        color = "32" if returncode == 0 else "31"
    _emit(_rule(f"{label} · {elapsed:.2f}s", color))
    _emit()


def _print_service_start(name, command, pid):
    _emit()
    _emit(_rule(f"{time.strftime('%H:%M:%S')} · service {name} · pid {pid}", "33"))
    _emit(_color(f"$ {command}", "1", "36"))


def _print_executor_banner(executor_id, broker_url):
    _emit()
    _emit(_color(f"Broker executor · {executor_id}", "1", "36"))
    for label, value in (
        ("cwd", os.getcwd()),
        ("python", sys.executable),
        ("shell", _get_shell()),
        ("broker", broker_url),
    ):
        _emit(_color(f"  {label:<7} {value}", "2"))
    _emit()


OUTPUT_CHUNK_BYTES = 65536


async def _read_output(stream):
    """Yield process output as soon as it is produced.

    Reading raw chunks instead of lines means output without a trailing
    newline (progress bars, carriage-return updates, partial lines) is
    forwarded immediately instead of being held back until the next
    newline or EOF. The incremental decoder keeps multi-byte characters
    intact when one straddles a chunk boundary.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = await stream.read(OUTPUT_CHUNK_BYTES)
        if not chunk:
            break
        text = decoder.decode(chunk)
        if text:
            yield text
    trailing = decoder.decode(b"", final=True)
    if trailing:
        yield trailing


async def _execute(websocket, event, task):
    task_id = event["task_id"]
    started = time.time()
    logger.debug("Command started: task_id=%s command=%r", task_id, event["command"])
    received_at = task.get("received_at")
    waited = time.monotonic() - received_at if received_at is not None else None
    _print_command_start(event["command"], task_id, waited)
    process = await asyncio.create_subprocess_shell(
        event["command"],
        cwd=os.getcwd(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        executable=_get_shell(),
        start_new_session=True,
    )
    task["process"] = process
    if task["cancel_requested"]:
        _cancel_task(task)
    await websocket.send(json.dumps(message("task_started", task_id=task_id)))

    async def forward(stream, name):
        writer = _OutputWriter(f"task {task_id[:8]}", name)
        async for text in _read_output(stream):
            writer.write(text)
            await websocket.send(
                json.dumps(
                    message(
                        "output",
                        task_id=task_id,
                        stream=name,
                        data=text,
                    )
                )
            )

    await asyncio.gather(forward(process.stdout, "stdout"), forward(process.stderr, "stderr"))
    returncode = await process.wait()
    _print_command_end(returncode, time.time() - started, task.get("cancel_reason"))
    await websocket.send(
        json.dumps(message(
            "result", task_id=task_id, returncode=returncode,
            status="cancelled" if task["cancel_requested"] else "completed",
        ))
    )
    logger.debug("Command completed: task_id=%s returncode=%s", task_id, returncode)


def _cancel_task(task, reason="requested"):
    task["cancel_requested"] = True
    task.setdefault("cancel_reason", reason)
    process = task["process"]
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def _run_task(websocket, event, task, tasks, execution_lock):
    try:
        async with execution_lock:
            if not task["cancel_requested"]:
                task["started"] = True
                await _execute(websocket, event, task)
    except Exception as exc:
        _cancel_task(task, reason="execution failed")
        if task["process"] is not None:
            await task["process"].wait()
        logger.exception("Command failed: task_id=%s", event["task_id"])
        try:
            await websocket.send(json.dumps(message(
                "result", task_id=event["task_id"], returncode=None,
                status="failed", error=str(exc),
            )))
        except ConnectionClosed:
            pass
    finally:
        tasks.pop(event["task_id"], None)


async def _send_service_status(websocket, service_id, status, **fields):
    await websocket.send(
        json.dumps(message("service_status", service_id=service_id, status=status, **fields))
    )


async def _forward_service_output(websocket, service_id, stream, name, service_name=None):
    writer = _OutputWriter(f"svc {service_name or service_id[:8]}", name)
    async for text in _read_output(stream):
        # Services commonly write normal logs to stderr; preserve the stream
        # destination without coloring the content as an error.
        writer.write(text)
        await websocket.send(
            json.dumps(
                message(
                    "service_output",
                    service_id=service_id,
                    stream=name,
                    data=text,
                )
            )
        )


async def _wait_for_tcp_ready(process, ready_check, timeout):
    host = ready_check.get("host", "127.0.0.1")
    port = ready_check["port"]
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None or getattr(process, "_broker_stop_requested", False):
            return False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=1
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(1)
    return False


async def _monitor_service(websocket, service_id, name, process, ready_check, startup_timeout):
    stdout_task = asyncio.create_task(
        _forward_service_output(websocket, service_id, process.stdout, "stdout", name)
    )
    stderr_task = asyncio.create_task(
        _forward_service_output(websocket, service_id, process.stderr, "stderr", name)
    )
    status = "failed"
    error = None
    try:
        if ready_check:
            ready = await _wait_for_tcp_ready(process, ready_check, startup_timeout)
            if not ready and not getattr(process, "_broker_stop_requested", False):
                if process.returncode is not None:
                    error = "Service exited before becoming ready"
                else:
                    error = "Service did not become ready before timeout"
                await _terminate_services({service_id: process})
        if error is None:
            if not getattr(process, "_broker_stop_requested", False):
                await _send_service_status(websocket, service_id, "ready", pid=process.pid)
                _emit(_rule(f"service {name} ready · pid {process.pid}", "32"))
            returncode = await process.wait()
            status = "stopped" if returncode == 0 or getattr(process, "_broker_stop_requested", False) else "failed"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = f"Service monitoring failed: {exc}"
        await _terminate_services({service_id: process})
    finally:
        output_results = await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    if any(isinstance(result, Exception) for result in output_results):
        status = "failed"
        error = error or "Service output forwarding failed"
    fields = {"pid": process.pid, "returncode": process.returncode}
    if error:
        fields["error"] = error
    _emit(_rule(f"service {name} {status} · exit {process.returncode}", "32" if status == "stopped" else "31"))
    if error:
        _emit(_color(f"  reason: {error}", "31"))
    await _send_service_status(websocket, service_id, status, **fields)


async def _start_service(websocket, event, services, monitor_tasks=None):
    try:
        validate_service(event.get("name"), event.get("command"), event.get("ready_check"), event.get("startup_timeout", 120))
        process = await asyncio.create_subprocess_shell(
            event["command"],
            cwd=os.getcwd(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            executable="/bin/bash",
            start_new_session=True,
        )
    except Exception as exc:
        error = f"Service startup failed: {exc}"
        name = event.get("name") or event["service_id"]
        _emit(_rule(f"service {name} failed", "31"))
        _emit(_color(f"  reason: {error}", "31"))
        await _send_service_status(websocket, event["service_id"], "failed", returncode=None, error=error)
        return
    services[event["service_id"]] = process
    service_name = event.get("name") or event["service_id"]
    _print_service_start(service_name, event["command"], process.pid)
    await _send_service_status(websocket, event["service_id"], "starting", pid=process.pid)
    monitor_task = asyncio.create_task(
        _monitor_service(
            websocket,
            event["service_id"],
            service_name,
            process,
            event.get("ready_check"),
            event.get("startup_timeout", 120),
        )
    )
    if monitor_tasks is not None:
        monitor_tasks.add(monitor_task)
        monitor_task.add_done_callback(monitor_tasks.discard)
    monitor_task.add_done_callback(lambda _task: services.pop(event["service_id"], None))
    logger.debug("Service started: id=%s pid=%s command=%r", event["service_id"], process.pid, event["command"])


async def _stop_service(websocket, event, services):
    process = services.get(event["service_id"])
    if process is None or process.returncode is not None:
        return
    if getattr(process, "_broker_stop_requested", False):
        return
    process._broker_stop_requested = True
    await _send_service_status(websocket, event["service_id"], "stopping", pid=process.pid)
    await _terminate_services({event["service_id"]: process})


async def _terminate_services(services):
    """Terminate all service process groups and reap their subprocesses."""
    processes = [process for process in services.values() if process.returncode is None]
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            # The process may have exited between the returncode check and killpg.
            pass
    try:
        await asyncio.wait_for(
            asyncio.gather(*(process.wait() for process in processes)), timeout=5
        )
    except asyncio.TimeoutError:
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.gather(*(process.wait() for process in processes), return_exceptions=True)
    return len(processes)


class BrokerConnectionError(ConnectionError):
    """The executor could not establish a connection to its broker."""


async def _run_executor_connection(websocket, broker_url, state, metadata, interactive):
    """Register and serve one connection, cleaning up before any reconnect."""
    executor_id = state["executor_id"]
    services = {}
    monitor_tasks = set()
    tasks = {}
    execution_tasks = set()
    execution_lock = asyncio.Lock()
    async with websocket:
        while True:
            logger.info("Connecting to broker: url=%s executor_id=%s", broker_url, executor_id)
            await websocket.send(
                json.dumps(
                    {
                        **message(
                            "register",
                            executor_id=executor_id,
                            cwd=os.getcwd(),
                            python=os.sys.executable,
                            shell=_get_shell(),
                        ),
                        **metadata,
                        "version": __version__,
                    }
                )
            )
            registration_result = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
            if registration_result.get("accepted"):
                warn_version_mismatch(logger, registration_result.get("version"), __version__)
                state["executor_id"] = executor_id
                state["registered"] = True
                state["registered_at"] = asyncio.get_running_loop().time()
                break
            if state["registered"]:
                raise _RegistrationRetry(registration_result.get("reason", "Registration rejected"))
            if not interactive:
                raise RuntimeError(registration_result.get("reason", "Executor registration rejected"))
            print(registration_result.get("reason", "Executor name rejected"))
            executor_id = input("Enter a different executor name: ").strip()
            while not executor_id:
                executor_id = input("Executor name cannot be empty. Enter a name: ").strip()

        logger.debug("Executor registered: id=%s cwd=%s python=%s", executor_id, os.getcwd(), os.sys.executable)
        _print_executor_banner(executor_id, broker_url)
        try:
            async for raw_message in websocket:
                event = json.loads(raw_message)
                if event.get("type") == "execute":
                    task = {"process": None, "cancel_requested": False, "started": False,
                            "received_at": time.monotonic()}
                    tasks[event["task_id"]] = task
                    _emit(_rule(f"{time.strftime('%H:%M:%S')} · task {event['task_id'][:8]} QUEUED", "2"))
                    execution_task = asyncio.create_task(
                        _run_task(websocket, event, task, tasks, execution_lock)
                    )
                    execution_tasks.add(execution_task)
                    execution_task.add_done_callback(execution_tasks.discard)
                elif event.get("type") == "cancel_task":
                    task = tasks.get(event["task_id"])
                    if task is not None:
                        _cancel_task(task)
                        if not task["started"]:
                            _emit(_rule(f"task {event['task_id'][:8]} CANCELLED · before launch", "33"))
                            await websocket.send(json.dumps(message(
                                "result", task_id=event["task_id"],
                                returncode=None, status="cancelled",
                            )))
                elif event.get("type") == "start_service":
                    await _start_service(websocket, event, services, monitor_tasks)
                elif event.get("type") == "stop_service":
                    stop_task = asyncio.create_task(_stop_service(websocket, event, services))
                    monitor_tasks.add(stop_task)
                    stop_task.add_done_callback(monitor_tasks.discard)
        finally:
            for task in tasks.values():
                _cancel_task(task, reason="connection closed or executor shutting down")
            if execution_tasks:
                await asyncio.gather(*execution_tasks, return_exceptions=True)
            for monitor_task in monitor_tasks:
                monitor_task.cancel()
            service_count = await _terminate_services(services)
            if monitor_tasks:
                await asyncio.gather(*monitor_tasks, return_exceptions=True)
            logger.info("Executor stopped: id=%s services=%s", executor_id, service_count)


class _RegistrationRetry(ConnectionError):
    """The previous registration may still be online at the broker."""


async def run_executor(broker_url, executor_id, metadata=None, interactive=False, *, key, output_mode="raw"):
    """Run an executor with a local output mode retained across reconnects."""
    if output_mode not in {"raw", "prefixed"}:
        raise ValueError("output_mode must be raw or prefixed")
    token = _OUTPUT_MODE.set(output_mode)
    try:
        await _reconnect_executor(broker_url, executor_id, metadata, interactive, key=key)
    finally:
        _OUTPUT_MODE.reset(token)


async def _reconnect_executor(broker_url, executor_id, metadata=None, interactive=False, *, key):
    """Reconnect a fixed local environment with capped exponential backoff."""
    validate_key(key)
    state = {"executor_id": executor_id, "registered": False, "registered_at": None}
    delay = 1
    while True:
        state["registered_at"] = None
        try:
            try:
                websocket = await connect(broker_url, additional_headers={"Authorization": f"Bearer {key}"})
            except Exception as exc:
                if process_exception(exc) is not None:
                    raise BrokerConnectionError(str(exc)) from exc
                raise
            await _run_executor_connection(websocket, broker_url, state, metadata or {}, interactive)
            reason = "Broker closed the connection"
        except BrokerConnectionError:
            raise
        except (ConnectionClosed, OSError, TimeoutError) as exc:
            reason = str(exc)
        except InvalidHandshake as exc:
            if process_exception(exc) is not None:
                raise BrokerConnectionError(str(exc)) from exc
            reason = str(exc)
        registered_at = state["registered_at"]
        if registered_at is not None and asyncio.get_running_loop().time() - registered_at >= 30:
            delay = 1
        logger.warning(
            "Broker unavailable: url=%s executor_id=%s reason=%s; retrying in %ss. "
            "Ensure the central broker is running and its WebSocket address is reachable.",
            broker_url, state["executor_id"], reason, delay,
        )
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)
