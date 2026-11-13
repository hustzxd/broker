"""Same-port web dashboard backed by the broker's existing state and controls."""

from pathlib import Path
from collections import OrderedDict
import json

from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Route
from websockets.exceptions import ConnectionClosed

from .terminal_output import render_output
from .version import __version__


ASSETS = Path(__file__).with_name("web_assets")
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


def web_routes(broker):
    rendered_logs = OrderedDict()
    cache_bytes = 0
    max_cache_bytes = 8 * 1024 * 1024

    def cross_origin(request):
        origin = request.headers.get("origin")
        return origin and origin != f"{request.url.scheme}://{request.url.netloc}"

    async def clear_history(request):
        nonlocal cache_bytes
        if cross_origin(request):
            return JSONResponse({"error": "Cross-origin controls are not allowed"}, status_code=403, headers=HEADERS)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return JSONResponse({"error": "Request too large"}, status_code=413, headers=HEADERS)
        try:
            data = json.loads(body)
            executor_id = data.get("executor_id") if isinstance(data, dict) else None
        except ValueError:
            executor_id = None
        if not isinstance(executor_id, str) or not executor_id:
            return JSONResponse({"error": "executor_id is required"}, status_code=400, headers=HEADERS)
        # No await between the online check and deletion: a concurrent reconnect
        # cannot lose new tasks or services during this operation.
        if executor_id in broker.executors:
            return JSONResponse({"error": "Executor is online; history was not cleared"}, status_code=409, headers=HEADERS)
        tasks = [key for key, value in broker.tasks.items() if value["executor_id"] == executor_id]
        services = [key for key, value in broker.services.items() if value["executor_id"] == executor_id]
        for key in tasks:
            if not broker.tasks[key]["done"].is_set():
                return JSONResponse({"error": "Unfinished task exists; history was not cleared"}, status_code=409, headers=HEADERS)
        for key in services:
            if broker.services[key]["status"] not in {"stopped", "failed"}:
                return JSONResponse({"error": "Active service exists; history was not cleared"}, status_code=409, headers=HEADERS)
        for kind, keys, records in (("tasks", tasks, broker.tasks), ("services", services, broker.services)):
            for key in keys:
                records.pop(key)
                cached = rendered_logs.pop((kind, key), None)
                if cached:
                    cache_bytes -= cached[2]
        return JSONResponse({"tasks_deleted": len(tasks), "services_deleted": len(services)}, headers=HEADERS)

    async def page(request):
        if request.url.path == "/ui":
            return RedirectResponse("/ui/", headers=HEADERS)
        return FileResponse(ASSETS / "index.html", headers=HEADERS)

    async def asset(request):
        return FileResponse(ASSETS / request.url.path.rsplit("/", 1)[-1], headers=HEADERS)

    async def state(request):
        return JSONResponse({
            "version": __version__,
            "executors": broker.list_executors(),
            "tasks": broker.list_tasks(include_completed=True),
            "services": broker.list_services(include_inactive=True),
            "executor_paseo": broker.executor_paseo,
            "paseo_tests": broker.paseo_tests,
        }, headers=HEADERS)

    async def executor_paseo(request):
        if cross_origin(request):
            return JSONResponse({"error": "Cross-origin controls are not allowed"}, status_code=403, headers=HEADERS)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return JSONResponse({"error": "Request too large"}, status_code=413, headers=HEADERS)
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            executor_id = data.get("executor_id")
            broker._validate_binding_executor(executor_id)
            if request.url.path.endswith("/test"):
                notification = broker.enqueue_paseo_test(executor_id)
                return JSONResponse(notification, status_code=202, headers=HEADERS)
            if "paseo" not in data:
                raise ValueError("paseo is required; use null to remove a binding")
            binding = broker.set_executor_paseo(executor_id, data["paseo"])
            return JSONResponse({"executor_id": executor_id, "paseo": binding}, headers=HEADERS)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400, headers=HEADERS)
        except (OSError, RuntimeError):
            return JSONResponse({"error": "Could not save binding or contact Paseo; check the broker configuration and daemon connection"}, status_code=503, headers=HEADERS)

    async def logs(request):
        nonlocal cache_bytes
        # Evicted broker records must not keep their rendered logs alive.
        for cache_key in list(rendered_logs):
            records = broker.tasks if cache_key[0] == "tasks" else broker.services
            if cache_key[1] not in records:
                cache_bytes -= rendered_logs.pop(cache_key)[2]
        kind, run_id = request.path_params["kind"], request.path_params["run_id"]
        if kind not in {"tasks", "services"}:
            return JSONResponse({"error": "Unknown record type"}, status_code=404, headers=HEADERS)
        record = (broker.tasks if kind == "tasks" else broker.services).get(run_id)
        if record is None:
            return JSONResponse({"error": "Record is no longer retained"}, status_code=404, headers=HEADERS)
        buffer = record["output" if kind == "tasks" else "logs"]
        revision = str(buffer.total)
        if request.query_params.get("revision") == revision:
            return JSONResponse({"revision": revision, "unchanged": True}, headers=HEADERS)
        cache_key = (kind, run_id)
        cached = rendered_logs.get(cache_key)
        if cached is not None and cached[0] == revision:
            rendered_logs.move_to_end(cache_key)
            return JSONResponse(cached[1], headers=HEADERS)
        result = buffer.read()
        text = render_output("".join(item["data"] for item in result["entries"]))
        payload = {"text": text, "truncated": result["truncated"], "revision": revision}
        size = len(text.encode("utf-8"))
        if cached is not None:
            cache_bytes -= rendered_logs.pop(cache_key)[2]
        if size <= max_cache_bytes:
            rendered_logs[cache_key] = (revision, payload, size)
            cache_bytes += size
            while cache_bytes > max_cache_bytes or len(rendered_logs) > 32:
                cache_bytes -= rendered_logs.popitem(last=False)[1][2]
        return JSONResponse(payload, headers=HEADERS)

    async def control(request):
        if request.path_params["kind"] not in {"tasks", "services"}:
            return JSONResponse({"error": "Unknown record type"}, status_code=404, headers=HEADERS)
        if cross_origin(request):
            return JSONResponse({"error": "Cross-origin controls are not allowed"}, status_code=403, headers=HEADERS)
        run_id = request.path_params["run_id"]
        try:
            if request.path_params["kind"] == "tasks":
                result = await broker.cancel_task(run_id)
            else:
                result = await broker.stop_service(run_id)
        except ToolError as error:
            return JSONResponse({"error": str(error)}, status_code=400, headers=HEADERS)
        except (ConnectionClosed, OSError):
            return JSONResponse({"error": "Executor connection unavailable"}, status_code=503, headers=HEADERS)
        return JSONResponse({"status": result["status"]}, headers=HEADERS)

    return [
        Route("/ui", page), Route("/ui/", page),
        Route("/ui/app.js", asset), Route("/ui/style.css", asset),
        Route("/ui/favicon.svg", asset),
        Route("/ui/api/state", state),
        Route("/ui/api/executor-paseo", executor_paseo, methods=["POST"]),
        Route("/ui/api/executor-paseo/test", executor_paseo, methods=["POST"]),
        Route("/ui/api/history/clear", clear_history, methods=["POST"]),
        Route("/ui/api/{kind}/{run_id}/logs", logs),
        Route("/ui/api/{kind}/{run_id}/stop", control, methods=["POST"]),
    ]
