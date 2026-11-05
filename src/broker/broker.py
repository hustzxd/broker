"""MCP-facing execution broker."""

from contextlib import asynccontextmanager
import asyncio
import json
import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from websockets.asyncio.server import serve

from .protocol import message, request_id

logger = logging.getLogger(__name__)
ACTIVE_SERVICE_STATUSES = {"starting", "ready", "stopping"}
TERMINAL_SERVICE_STATUSES = {"stopped", "failed"}


def _raise_tool_error(code, message_text):
    raise ToolError(f"[{code}] {message_text}")


class ExecutionBroker:
    def __init__(self):
        self.executors = {}
        self.tasks = {}
        self.services = {}

    async def shutdown(self):
        connections = [executor["websocket"] for executor in self.executors.values()]
        if connections:
            await asyncio.gather(*(connection.close() for connection in connections))
        self.executors.clear()

    async def handle_executor(self, websocket):
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

        self.executors[executor_id] = {"websocket": websocket, **registration}
        await websocket.send(
            json.dumps(message("register_result", accepted=True, executor_id=executor_id))
        )
        logger.info(
            "Executor connected: id=%s host=%s cwd=%s python=%s",
            executor_id,
            registration.get("host", "unknown"),
            registration.get("cwd", "unknown"),
            registration.get("python", "unknown"),
        )
        try:
            async for raw_message in websocket:
                event = json.loads(raw_message)
                if event.get("type") == "heartbeat":
                    self.executors[executor_id]["last_heartbeat"] = event.get("timestamp")
                elif event.get("type") == "output":
                    task = self.tasks.get(event.get("task_id"))
                    if task is not None:
                        task["output"].append(
                            {"stream": event["stream"], "data": event["data"]}
                        )
                elif event.get("type") == "result":
                    task = self.tasks.get(event.get("task_id"))
                    if task is not None:
                        task["status"] = "completed"
                        task["returncode"] = event["returncode"]
                        task["done"].set()
                elif event.get("type") == "service_status":
                    service_id = event.get("service_id")
                    service = self.services.get(service_id)
                    if service is not None:
                        service.update(
                            {
                                key: event[key]
                                for key in ("status", "pid", "returncode", "error")
                                if key in event
                            }
                        )
                        if event.get("status") in TERMINAL_SERVICE_STATUSES:
                            self.services.pop(service_id, None)
                elif event.get("type") == "service_output":
                    service = self.services.get(event.get("service_id"))
                    if service is not None:
                        service["logs"].append(
                            {"stream": event["stream"], "data": event["data"]}
                        )
        finally:
            self.executors.pop(executor_id, None)
            for service_id, service in list(self.services.items()):
                if service["executor_id"] == executor_id:
                    self.services.pop(service_id, None)
            logger.info("Executor disconnected: id=%s", executor_id)

    def list_executors(self):
        return [
            {key: value for key, value in executor.items() if key != "websocket"}
            for executor in self.executors.values()
        ]

    async def execute(self, executor_id, command, timeout=60):
        executor = self.executors.get(executor_id)
        if executor is None:
            _raise_tool_error("unknown_executor", f"Unknown executor: {executor_id}")
        task_id = request_id()
        self.tasks[task_id] = {
            "task_id": task_id,
            "executor_id": executor_id,
            "command": command,
            "status": "submitted",
            "output": [],
            "done": asyncio.Event(),
        }
        await executor["websocket"].send(
            json.dumps(message("execute", task_id=task_id, command=command))
        )
        logger.info(
            "Command submitted: task_id=%s executor_id=%s command=%r",
            task_id,
            executor_id,
            command,
        )
        try:
            await asyncio.wait_for(self.tasks[task_id]["done"].wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"task_id": task_id, "status": "running"}
        return self.get_task(task_id)

    def get_task(self, task_id):
        task = self.tasks.get(task_id)
        if task is None:
            _raise_tool_error("unknown_task", f"Unknown task: {task_id}")
        return {key: value for key, value in task.items() if key != "done"}

    async def start_service(self, executor_id, name, command, ready_check=None, startup_timeout=120):
        import json

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
        self.services[service_id] = {
            "service_id": service_id,
            "executor_id": executor_id,
            "name": name,
            "command": command,
            "status": "starting",
            "logs": [],
        }
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

    def get_service_logs(self, service_id, tail=200):
        service = self.services.get(service_id)
        if service is None:
            _raise_tool_error("unknown_service", f"Unknown service: {service_id}")
        return service["logs"][-tail:]

    async def stop_service(self, service_id):
        import json

        service = self.services.get(service_id)
        if service is None:
            _raise_tool_error("unknown_service", f"Unknown service: {service_id}")
        executor = self.executors.get(service["executor_id"])
        if executor is None:
            _raise_tool_error("executor_offline", f"Executor is offline: {service['executor_id']}")
        await executor["websocket"].send(
            json.dumps(message("stop_service", service_id=service_id))
        )
        service["status"] = "stopping"
        return {"service_id": service_id, "status": "stopping"}


def create_broker(
    executor_host="127.0.0.1",
    executor_port=8272,
):
    broker = ExecutionBroker()

    @asynccontextmanager
    async def lifespan(_server):
        async with serve(broker.handle_executor, executor_host, executor_port):
            try:
                yield broker
            finally:
                await broker.shutdown()

    # log_level="WARNING" silences uvicorn's INFO access log, which otherwise
    # spams 404s for the OAuth discovery probes (/.well-known/oauth-*) that MCP
    # clients send before falling back to unauthenticated HTTP.
    mcp = MCPServer("execution-broker", lifespan=lifespan, log_level="WARNING")

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
    def get_task(task_id: str) -> dict:
        """Get the status and output of a submitted command."""
        return broker.get_task(task_id)

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
    def get_service_logs(service_id: str, tail: int = 200) -> list[dict]:
        """Get recent stdout and stderr output for a service."""
        return broker.get_service_logs(service_id, tail)

    @mcp.tool()
    async def stop_service(service_id: str) -> dict:
        """Stop a long-running service."""
        return await broker.stop_service(service_id)

    return mcp
