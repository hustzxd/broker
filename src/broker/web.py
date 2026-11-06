"""Same-port web dashboard backed by the broker's existing state and controls."""

from pathlib import Path

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
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


def web_routes(broker):
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
        }, headers=HEADERS)

    async def logs(request):
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
        result = buffer.read()
        text = render_output("".join(item["data"] for item in result["entries"]))
        return JSONResponse({"text": text, "truncated": result["truncated"], "revision": revision}, headers=HEADERS)

    async def control(request):
        if request.path_params["kind"] not in {"tasks", "services"}:
            return JSONResponse({"error": "Unknown record type"}, status_code=404, headers=HEADERS)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
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
        Route("/ui/api/state", state),
        Route("/ui/api/{kind}/{run_id}/logs", logs),
        Route("/ui/api/{kind}/{run_id}/stop", control, methods=["POST"]),
    ]
