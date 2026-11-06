"""Read-only MCP connection diagnostics without exposing credentials."""

import asyncio

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .auth import validate_key


def _leaf_errors(error):
    nested = getattr(error, "exceptions", None)
    if nested:
        return [leaf for child in nested for leaf in _leaf_errors(child)]
    return [error]


async def diagnose(url, key, timeout=10):
    validate_key(key)
    observed_statuses = []

    async def observe_response(response):
        if response.status_code >= 400:
            observed_statuses.append(response.status_code)

    async def check():
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {key}"}, timeout=timeout,
            event_hooks={"response": [observe_response]},
        ) as client:
            async with streamable_http_client(url, http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    initialization = await session.initialize()
                    tools = await session.list_tools()
                    return {
                        "ok": True, "status": "connected",
                        "server_version": initialization.server_info.version,
                        "tools": [tool.name for tool in tools.tools],
                    }

    try:
        return await asyncio.wait_for(check(), timeout=timeout)
    except Exception as error:
        errors = _leaf_errors(error)
        statuses = observed_statuses + [exc.response.status_code for exc in errors if isinstance(exc, httpx.HTTPStatusError)]
        if any(status in (401, 403) for status in statuses):
            return {"ok": False, "status": "authentication_failed", "message": "Key rejected (401/403); check BROKER_KEY or broker.key and the client's Authorization header."}
        if any(isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)) for exc in errors):
            return {"ok": False, "status": "timeout", "message": "Connection or MCP initialization timed out."}
        if any(isinstance(exc, httpx.ConnectError) for exc in errors):
            return {"ok": False, "status": "connection_failed", "message": "Cannot connect; check the broker process, address, port and TLS certificate."}
        if statuses:
            return {"ok": False, "status": "http_error", "message": f"HTTP {statuses[0]}; check the /mcp path and reverse proxy."}
        return {"ok": False, "status": "protocol_error", "message": "MCP initialization or tool discovery failed; check server compatibility and proxy configuration."}
