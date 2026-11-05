"""Executor daemon."""

import asyncio
import codecs
import json
import logging
import os
import signal
import sys
import time

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidHandshake, InvalidURI

from .protocol import message

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


def _color(text, *codes):
    """Wrap text in ANSI codes when the console is a terminal."""
    if not _COLOR or not codes:
        return text
    return "".join(f"\033[{code}m" for code in codes) + text + "\033[0m"


def _emit(line=""):
    print(line, flush=True)


def _rule(label="", *codes):
    if label:
        fill = "─" * max(1, RULE_WIDTH - len(label) - 6)
        text = f"──── {label} {fill}"
    else:
        text = "─" * RULE_WIDTH
    return _color(text, *codes)


def _print_command_start(command, task_id):
    _emit()
    _emit(_rule(f"{time.strftime('%H:%M:%S')} · task {task_id[:8]}", "2"))
    _emit(_color(f"$ {command}", "1", "36"))


def _print_command_end(returncode, elapsed):
    label = "exit 0" if returncode == 0 else f"exit {returncode}"
    _emit(_rule(f"{label} · {elapsed:.2f}s", "32" if returncode == 0 else "31"))
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


async def _execute(websocket, event):
    task_id = event["task_id"]
    started = time.time()
    logger.debug("Command started: task_id=%s command=%r", task_id, event["command"])
    _print_command_start(event["command"], task_id)
    process = await asyncio.create_subprocess_shell(
        event["command"],
        cwd=os.getcwd(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        executable=_get_shell(),
    )

    async def forward(stream, name):
        output_stream = sys.stdout if name == "stdout" else sys.stderr
        tint = () if name == "stdout" else ("31",)
        async for text in _read_output(stream):
            output_stream.write(_color(text, *tint))
            output_stream.flush()
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
    _print_command_end(returncode, time.time() - started)
    await websocket.send(
        json.dumps(message("result", task_id=task_id, returncode=returncode))
    )
    logger.debug("Command completed: task_id=%s returncode=%s", task_id, returncode)


async def _send_service_status(websocket, service_id, status, **fields):
    await websocket.send(
        json.dumps(message("service_status", service_id=service_id, status=status, **fields))
    )


async def _forward_service_output(websocket, service_id, stream, name):
    async for text in _read_output(stream):
        # Services commonly write normal logs to stderr; preserve the stream
        # destination without coloring the content as an error.
        output_stream = sys.stdout if name == "stdout" else sys.stderr
        output_stream.write(text)
        output_stream.flush()
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
        if process.returncode is not None:
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
        _forward_service_output(websocket, service_id, process.stdout, "stdout")
    )
    stderr_task = asyncio.create_task(
        _forward_service_output(websocket, service_id, process.stderr, "stderr")
    )
    try:
        if ready_check:
            if ready_check.get("type") != "tcp":
                _emit(_rule(f"service {name} failed · only tcp readiness checks are supported", "31"))
                await _send_service_status(
                    websocket,
                    service_id,
                    "failed",
                    error="Only tcp readiness checks are supported",
                )
                os.killpg(process.pid, signal.SIGTERM)
                await process.wait()
                return
            ready = await _wait_for_tcp_ready(process, ready_check, startup_timeout)
            if not ready:
                _emit(_rule(f"service {name} failed · not ready within {startup_timeout}s", "31"))
                await _send_service_status(
                    websocket,
                    service_id,
                    "failed",
                    error="Service did not become ready before timeout",
                )
                os.killpg(process.pid, signal.SIGTERM)
                await process.wait()
                return

        await _send_service_status(websocket, service_id, "ready", pid=process.pid)
        _emit(_rule(f"service {name} ready · pid {process.pid}", "32"))
        returncode = await process.wait()
        status = "stopped" if returncode == 0 else "failed"
        await _send_service_status(
            websocket, service_id, status, pid=process.pid, returncode=returncode
        )
        _emit(
            _rule(
                f"service {name} {status} · exit {returncode}",
                "32" if status == "stopped" else "31",
            )
        )
        _emit()
    finally:
        await asyncio.gather(stdout_task, stderr_task)


async def _start_service(websocket, event, services, monitor_tasks=None):
    process = await asyncio.create_subprocess_shell(
        event["command"],
        cwd=os.getcwd(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        executable="/bin/bash",
        start_new_session=True,
    )
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
    logger.debug("Service started: id=%s pid=%s command=%r", event["service_id"], process.pid, event["command"])


async def _stop_service(websocket, event, services):
    process = services.get(event["service_id"])
    if process is None or process.returncode is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    await _send_service_status(websocket, event["service_id"], "stopping", pid=process.pid)


async def _terminate_services(services):
    """Terminate all service process groups and reap their subprocesses."""
    processes = [process for process in services.values() if process.returncode is None]
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            # The process may have exited between the returncode check and killpg.
            pass
    await asyncio.gather(*(process.wait() for process in processes), return_exceptions=True)
    return len(processes)


class BrokerConnectionError(ConnectionError):
    """The executor could not establish a connection to its broker."""


async def run_executor(broker_url, executor_id, metadata=None, interactive=False):
    """Connect a fixed local environment to an execution broker."""
    metadata = metadata or {}
    services = {}
    monitor_tasks = set()
    try:
        websocket = await connect(broker_url)
    except (OSError, TimeoutError, InvalidHandshake, InvalidURI) as exc:
        raise BrokerConnectionError(str(exc)) from exc
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
                    }
                )
            )
            registration_result = json.loads(await websocket.recv())
            if registration_result.get("accepted"):
                break
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
                    await _execute(websocket, event)
                elif event.get("type") == "start_service":
                    await _start_service(websocket, event, services, monitor_tasks)
                elif event.get("type") == "stop_service":
                    await _stop_service(websocket, event, services)
        finally:
            service_count = await _terminate_services(services)
            if monitor_tasks:
                await asyncio.gather(*monitor_tasks, return_exceptions=True)
            logger.info("Executor stopped: id=%s services=%s", executor_id, service_count)
