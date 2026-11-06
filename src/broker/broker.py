"""MCP-facing execution broker."""

from contextlib import asynccontextmanager
import asyncio
import json
import logging
import time

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from .protocol import message, request_id, validate_service
from .logs import DEFAULT_MAX_LOG_BYTES, LogBuffer
from .auth import BearerKeyMiddleware, executor_auth, validate_key
from .version import __version__, warn_version_mismatch

logger = logging.getLogger(__name__)
ACTIVE_SERVICE_STATUSES = {"starting", "ready", "stopping"}
TERMINAL_SERVICE_STATUSES = {"stopped", "failed"}


def _raise_tool_error(code, message_text):
    raise ToolError(f"[{code}] {message_text}")


class ExecutionBroker:
    def __init__(self, max_log_bytes=DEFAULT_MAX_LOG_BYTES, max_completed_services=100,
                 max_completed_tasks=100, max_pending_tasks=32):
        if isinstance(max_log_bytes, bool) or not isinstance(max_log_bytes, int) or max_log_bytes <= 0:
            raise ValueError("max_log_bytes must be a positive integer")
        self.max_log_bytes = max_log_bytes
        if isinstance(max_completed_services, bool) or not isinstance(max_completed_services, int) or max_completed_services < 1:
            raise ValueError("max_completed_services must be a positive integer")
        self.max_completed_services = max_completed_services
        for name, value in (("max_completed_tasks", max_completed_tasks), ("max_pending_tasks", max_pending_tasks)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.max_completed_tasks = max_completed_tasks
        self.max_pending_tasks = max_pending_tasks
        self.executors = {}
        self.tasks = {}
        self.services = {}

    async def shutdown(self):
        connections = [executor["websocket"] for executor in self.executors.values()]
        if connections:
            await asyncio.gather(*(connection.close() for connection in connections))
        self.executors.clear()

    async def handle_executor(self, websocket):
        try:
            await self._handle_executor(websocket)
        except ConnectionClosed as exc:
            logger.info("Executor connection closed: %s", exc)

    async def _handle_executor(self, websocket):
        executor_id = None
        while executor_id is None:
            registration = await websocket.recv()
            if not isinstance(registration, str):
                return
            registration = json.loads(registration)
            if registration.get("type") != "register":
                return
            requested_id = registration.get("executor_id")
            if not requested_id:
                return
            if requested_id in self.executors:
                await websocket.send(
                    json.dumps(
                        message(
                            "register_result",
                            accepted=False,
                            reason=f"Executor name already exists: {requested_id}",
                        )
                    )
                )
                logger.warning("Executor name is already in use: id=%s", requested_id)
                continue
            executor_id = requested_id

        self.executors[executor_id] = {
            **{key: registration[key] for key in ("executor_id", "cwd", "python", "shell", "host", "version") if key in registration},
            "websocket": websocket,
        }
        try:
            warn_version_mismatch(logger, __version__, registration.get("version"))
            await websocket.send(
                json.dumps(message("register_result", accepted=True, executor_id=executor_id, version=__version__))
            )
            logger.info(
                "Executor connected: id=%s host=%s cwd=%s python=%s",
                executor_id,
                registration.get("host", "unknown"),
                registration.get("cwd", "unknown"),
                registration.get("python", "unknown"),
            )
            async for raw_message in websocket:
                event = json.loads(raw_message)
                if event.get("type") == "heartbeat":
                    self.executors[executor_id]["last_heartbeat"] = event.get("timestamp")
                elif event.get("type") == "task_started":
                    task = self.tasks.get(event.get("task_id"))
                    if task is not None and not task["done"].is_set() and task["executor_id"] == executor_id:
                        task["started_at"] = time.time()
                        if task["status"] != "cancelling":
                            task["status"] = "running"
                elif event.get("type") == "output":
                    task = self.tasks.get(event.get("task_id"))
                    if task is not None and not task["done"].is_set() and task["executor_id"] == executor_id:
                        task["output"].append(event["stream"], event["data"])
                elif event.get("type") == "result":
                    task = self.tasks.get(event.get("task_id"))
                    if task is not None and not task["done"].is_set() and task["executor_id"] == executor_id:
                        task["status"] = event.get("status", "completed")
                        task["returncode"] = event["returncode"]
                        if "error" in event:
                            task["error"] = event["error"]
                        task["finished_at"] = time.time()
                        task["done"].set()
                        self._prune_tasks()
                elif event.get("type") == "service_status":
                    service_id = event.get("service_id")
                    service = self.services.get(service_id)
                    if (service is not None and service["executor_id"] == executor_id
                            and service["status"] not in TERMINAL_SERVICE_STATUSES
                            and not (service["status"] == "stopping" and event.get("status") in {"starting", "ready"})):
                        service.update(
                            {
                                key: event[key]
                                for key in ("status", "pid", "returncode", "error")
                                if key in event
                            }
                        )
                        if event.get("status") in TERMINAL_SERVICE_STATUSES:
                            service["finished_at"] = time.time()
                            self._prune_services()
                elif event.get("type") == "service_output":
                    service = self.services.get(event.get("service_id"))
                    if service is not None and service["executor_id"] == executor_id:
                        service["logs"].append(event["stream"], event["data"])
        finally:
            self.executors.pop(executor_id, None)
            for task in list(self.tasks.values()):
                if task["executor_id"] == executor_id and not task["done"].is_set():
                    self._mark_task_lost(task)
            for service_id, service in list(self.services.items()):
                if service["executor_id"] == executor_id and service["status"] in ACTIVE_SERVICE_STATUSES:
                    service.update(status="failed", returncode=None, finished_at=time.time(),
                                   error="Executor disconnected; service outcome is unknown")
            self._prune_services()
            logger.info("Executor disconnected: id=%s", executor_id)

    def _mark_task_lost(self, task):
        task.update(
            status="lost", returncode=None, finished_at=time.time(),
            error="Executor disconnected before a result was received; execution outcome is unknown. Task will not be replayed.",
        )
        task["done"].set()
        self._prune_tasks()

    def _prune_tasks(self):
        completed = sorted(
            (task for task in self.tasks.values() if task["done"].is_set()),
            key=lambda task: task["finished_at"],
        )
        for task in completed[:-self.max_completed_tasks]:
            self.tasks.pop(task["task_id"], None)

    def _prune_services(self):
        completed = sorted(
            (service for service in self.services.values() if service["status"] in TERMINAL_SERVICE_STATUSES),
            key=lambda service: service["finished_at"],
        )
        for service in completed[:-self.max_completed_services]:
            self.services.pop(service["service_id"], None)

    def list_executors(self):
        return [
            {key: value for key, value in executor.items() if key != "websocket"}
            for executor in self.executors.values()
        ]

    async def execute(self, executor_id, command, timeout=60):
        executor = self.executors.get(executor_id)
        if executor is None:
            _raise_tool_error("unknown_executor", f"Unknown executor: {executor_id}")
        pending = sum(task["executor_id"] == executor_id and not task["done"].is_set() for task in self.tasks.values())
        if pending >= self.max_pending_tasks:
            _raise_tool_error("queue_full", f"Executor {executor_id} already has {pending} unfinished tasks")
        task_id = request_id()
        task = self.tasks[task_id] = {
            "task_id": task_id,
            "executor_id": executor_id,
            "command": command,
            "status": "submitted",
            "submitted_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "output": LogBuffer(self.max_log_bytes),
            "done": asyncio.Event(),
        }
        try:
            await executor["websocket"].send(
                json.dumps(message("execute", task_id=task_id, command=command))
            )
        except (ConnectionClosed, OSError):
            if not task["done"].is_set():
                self._mark_task_lost(task)
            return self._task_result(task)
        logger.info(
            "Command submitted: task_id=%s executor_id=%s command=%r",
            task_id,
            executor_id,
            command,
        )
        try:
            await asyncio.wait_for(task["done"].wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"task_id": task_id, "status": task["status"]}
        return self._task_result(task)

    def list_tasks(self, executor_id=None, include_completed=False):
        return [
            {key: value for key, value in task.items() if key not in {"done", "output"}}
            for task in self.tasks.values()
            if (executor_id is None or task["executor_id"] == executor_id)
            and (include_completed or not task["done"].is_set())
        ]

    def get_task(self, task_id, cursor=None):
        task = self.tasks.get(task_id)
        if task is None:
            _raise_tool_error("unknown_task", f"Unknown task: {task_id}")
        return self._task_result(task, cursor)

    def _task_result(self, task, cursor=None):
        try:
            logs = task["output"].read(cursor=cursor)
        except ValueError as exc:
            _raise_tool_error("invalid_cursor", str(exc))
        return {
            **{key: value for key, value in task.items() if key not in {"done", "output"}},
            "output": logs.pop("entries"),
            **logs,
        }

    async def cancel_task(self, task_id):
        task = self.tasks.get(task_id)
        if task is None:
            _raise_tool_error("unknown_task", f"Unknown task: {task_id}")
        if task["done"].is_set() or task["status"] == "cancelling":
            return self.get_task(task_id)
        executor = self.executors.get(task["executor_id"])
        if executor is None:
            _raise_tool_error("executor_offline", f"Executor is offline: {task['executor_id']}")
        previous_status = task["status"]
        task["status"] = "cancelling"
        try:
            await executor["websocket"].send(
                json.dumps(message("cancel_task", task_id=task_id))
            )
        except Exception:
            if not task["done"].is_set():
                task["status"] = previous_status
            raise
        return self._task_result(task)

    async def start_service(self, executor_id, name, command, ready_check=None, startup_timeout=120):
        try:
            validate_service(name, command, ready_check, startup_timeout)
        except ValueError as exc:
            _raise_tool_error("invalid_service", str(exc))

        executor = self.executors.get(executor_id)
        if executor is None:
            _raise_tool_error("unknown_executor", f"Unknown executor: {executor_id}")
        for service in self.services.values():
            if (
                service["executor_id"] == executor_id
                and service["name"] == name
                and service["status"] in ACTIVE_SERVICE_STATUSES
            ):
                _raise_tool_error("duplicate_service", f"Service name already exists: {name}")

        service_id = request_id()
        service = self.services[service_id] = {
            "service_id": service_id,
            "executor_id": executor_id,
            "name": name,
            "command": command,
            "status": "starting",
            "logs": LogBuffer(self.max_log_bytes),
        }
        try:
            await executor["websocket"].send(
                json.dumps(
                    message(
                        "start_service",
                        service_id=service_id,
                        name=name,
                        command=command,
                        ready_check=ready_check,
                        startup_timeout=startup_timeout,
                    )
                )
            )
        except (ConnectionClosed, OSError):
            service.update(
                status="failed", returncode=None, finished_at=time.time(),
                error="Service submission failed; executor connection lost",
            )
            self._prune_services()
            return {key: value for key, value in service.items() if key != "logs"}
        logger.info("Service submitted: id=%s executor_id=%s name=%s", service_id, executor_id, name)
        return {"service_id": service_id, "status": "starting"}

    def list_services(self, executor_id=None, include_inactive=False):
        services = self.services.values()
        if not include_inactive:
            services = (service for service in services if service["status"] in ACTIVE_SERVICE_STATUSES)
        if executor_id is not None:
            services = (service for service in services if service["executor_id"] == executor_id)
        return [{key: value for key, value in service.items() if key != "logs"} for service in services]

    def get_service(self, service_id):
        service = self.services.get(service_id)
        if service is None:
            _raise_tool_error("unknown_service", f"Unknown service: {service_id}")
        return {key: value for key, value in service.items() if key != "logs"}

    def get_service_logs(self, service_id, tail=200, cursor=None):
        service = self.services.get(service_id)
        if service is None:
            _raise_tool_error("unknown_service", f"Unknown service: {service_id}")
        if isinstance(tail, bool) or not isinstance(tail, int) or tail < 0:
            _raise_tool_error("invalid_tail", "tail must be a non-negative integer")
        try:
            logs = service["logs"].read(cursor=cursor, tail=tail)
        except ValueError as exc:
            _raise_tool_error("invalid_cursor", str(exc))
        return {"logs": logs.pop("entries"), **logs}

    async def stop_service(self, service_id):
        import json

        service = self.services.get(service_id)
        if service is None:
            _raise_tool_error("unknown_service", f"Unknown service: {service_id}")
        if service["status"] in TERMINAL_SERVICE_STATUSES or service["status"] == "stopping":
            return self.get_service(service_id)
        executor = self.executors.get(service["executor_id"])
        if executor is None:
            _raise_tool_error("executor_offline", f"Executor is offline: {service['executor_id']}")
        previous_status = service["status"]
        service["status"] = "stopping"
        try:
            await executor["websocket"].send(
                json.dumps(message("stop_service", service_id=service_id))
            )
        except Exception:
            if service["status"] == "stopping":
                service["status"] = previous_status
            raise
        return self.get_service(service_id)


class AuthenticatedMCPServer(MCPServer):
    def __init__(self, *args, key, **kwargs):
        super().__init__(*args, **kwargs)
        self._broker_key = validate_key(key)

    def streamable_http_app(self, **kwargs):
        app = super().streamable_http_app(**kwargs)
        app.add_middleware(BearerKeyMiddleware, key=self._broker_key)
        return app


def create_broker(
    executor_host="127.0.0.1",
    executor_port=8272,
    max_log_bytes=DEFAULT_MAX_LOG_BYTES,
    max_completed_services=100,
    max_completed_tasks=100,
    max_pending_tasks=32,
    *,
    key,
):
    validate_key(key)
    broker = ExecutionBroker(
        max_log_bytes=max_log_bytes, max_completed_services=max_completed_services,
        max_completed_tasks=max_completed_tasks, max_pending_tasks=max_pending_tasks,
    )

    @asynccontextmanager
    async def lifespan(_server):
        async with serve(
            broker.handle_executor, executor_host, executor_port,
            process_request=executor_auth(key),
        ):
            try:
                yield broker
            finally:
                await broker.shutdown()

    # Keep access logs quiet; authentication never logs request credentials.
    mcp = AuthenticatedMCPServer("execution-broker", key=key, version=__version__, lifespan=lifespan, log_level="WARNING")
    mcp.execution_broker = broker

    @mcp.tool()
    def list_executors() -> list[dict]:
        """List online executors."""
        return broker.list_executors()

    @mcp.tool()
    async def execute(
        executor_id: str,
        command: str,
        timeout: int = 60,
    ) -> dict:
        """Run a one-shot shell command and wait for its result."""
        return await broker.execute(executor_id, command, timeout)

    @mcp.tool()
    def list_tasks(executor_id: str | None = None, include_completed: bool = False) -> list[dict]:
        """List unfinished tasks; optionally include retained completed tasks. Output is queried separately."""
        return broker.list_tasks(executor_id, include_completed)

    @mcp.tool()
    def get_task(task_id: str, cursor: int | None = None) -> dict:
        """Get task status and output; pass next_cursor to read only new output."""
        return broker.get_task(task_id, cursor)

    @mcp.tool()
    async def cancel_task(task_id: str) -> dict:
        """Cancel a command and kill its process group; query get_task for completion."""
        return await broker.cancel_task(task_id)

    @mcp.tool()
    async def start_service(
        executor_id: str,
        name: str,
        command: str,
        ready_check: dict | None = None,
        startup_timeout: int = 120,
    ) -> dict:
        """Start a long-running service in an executor."""
        return await broker.start_service(
            executor_id, name, command, ready_check, startup_timeout
        )

    @mcp.tool()
    def list_services(
        executor_id: str | None = None,
        include_inactive: bool = False,
    ) -> list[dict]:
        """List active services; set include_inactive to include finished services."""
        return broker.list_services(executor_id, include_inactive)

    @mcp.tool()
    def get_service(service_id: str) -> dict:
        """Get the status of a long-running service."""
        return broker.get_service(service_id)

    @mcp.tool()
    def get_service_logs(service_id: str, tail: int = 200, cursor: int | None = None) -> dict:
        """Get service logs with truncation metadata; cursor overrides tail for incremental reads."""
        return broker.get_service_logs(service_id, tail, cursor)

    @mcp.tool()
    async def stop_service(service_id: str) -> dict:
        """Stop a long-running service."""
        return await broker.stop_service(service_id)

    return mcp
