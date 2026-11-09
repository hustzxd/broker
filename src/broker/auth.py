"""Shared-key authentication for MCP HTTP and executor WebSocket requests."""

import hmac
from http import HTTPStatus

from starlette.responses import JSONResponse


def validate_key(key):
    if not isinstance(key, str) or not key or not key.isascii() or not key.isprintable() or any(char.isspace() for char in key):
        raise ValueError("Broker key must be a non-empty ASCII string without whitespace")
    return key


def authorized(headers, key):
    # Reject ambiguous duplicate Authorization headers.
    if len(headers) != 1:
        return False
    scheme, separator, token = headers[0].partition(" ")
    return (
        scheme.lower() == "bearer" and bool(separator)
        and hmac.compare_digest(token.encode("utf-8"), key.encode("ascii"))
    )


class BearerKeyMiddleware:
    def __init__(self, app, key):
        self.app = app
        self.key = validate_key(key)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # Only the login page and its fixed assets are public; APIs and MCP
            # still authenticate every request, including existing sessions.
            if scope["method"] in {"GET", "HEAD"} and scope["path"] in {
                "/ui", "/ui/", "/ui/app.js", "/ui/style.css", "/ui/favicon.svg",
            }:
                await self.app(scope, receive, send)
                return
            headers = [value.decode("latin-1") for name, value in scope["headers"] if name.lower() == b"authorization"]
            if not authorized(headers, self.key):
                response = JSONResponse(
                    {"error": "Unauthorized"}, status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="broker"'},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def executor_auth(key):
    validate_key(key)

    async def process_request(connection, request):
        if authorized(request.headers.get_all("Authorization"), key):
            return None
        response = connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
        response.headers["WWW-Authenticate"] = 'Bearer realm="broker"'
        return response

    return process_request
