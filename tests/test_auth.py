import asyncio
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from broker.auth import executor_auth
from broker.broker import create_broker
from broker.config import ensure_broker_key, ensure_config, load_config
from broker.cli import main
from broker import executor
from test_cancel_task import MemorySocket


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_rejects_missing_wrong_and_duplicate_keys(self):
        app = create_broker(key="test-secret").streamable_http_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8273") as client:
            for headers in (
                {}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic test-secret"},
                [("Authorization", "Bearer test-secret"), ("Authorization", "Bearer wrong")],
            ):
                for method in ("GET", "POST", "DELETE"):
                    response = await client.request(method, "/mcp", headers=headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertNotIn("test-secret", response.text)
                    self.assertIn("Bearer", response.headers["WWW-Authenticate"])

    async def test_correct_key_can_initialize_and_call_mcp_tool(self):
        # The in-memory executor listener avoids binding network ports while
        # exercising real MCP initialization and HTTP tool calls via ASGI.
        class Listener:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        server = create_broker(key="test-secret")
        app = server.streamable_http_app(stateless_http=True, json_response=True)
        with patch("broker.broker.serve", return_value=Listener()):
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8273",
                    headers={"Authorization": "Bearer test-secret"},
                ) as client:
                    async with streamable_http_client("http://127.0.0.1:8273/mcp", http_client=client) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool("list_executors", {})
                            self.assertFalse(result.is_error)
                            self.assertEqual(result.structured_content, {"result": []})

    async def test_session_id_cannot_bypass_request_authentication(self):
        class Listener:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        app = create_broker(key="test-secret").streamable_http_app(json_response=True)
        with patch("broker.broker.serve", return_value=Listener()):
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8273",
                ) as client:
                    response = await client.post("/mcp", headers={
                        "Authorization": "Bearer test-secret", "Accept": "application/json, text/event-stream",
                    }, json={
                        "jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "1"}},
                    })
                    self.assertEqual(response.status_code, 200)
                    session_id = response.headers["mcp-session-id"]
                    rejected = await client.post("/mcp", headers={"Mcp-Session-Id": session_id}, json={
                        "jsonrpc": "2.0", "id": 2, "method": "tools/list",
                    })
                    self.assertEqual(rejected.status_code, 401)

    async def test_websocket_auth_rejects_before_registration(self):
        authenticate = executor_auth("test-secret")

        def respond(status, text):
            return Response(status, "Unauthorized", Headers(), text.encode())

        connection = SimpleNamespace(respond=respond)
        for headers in (
            Headers(), Headers({"Authorization": "Bearer wrong"}),
            Headers([("Authorization", "Bearer test-secret"), ("Authorization", "Bearer wrong")]),
        ):
            response = await authenticate(connection, SimpleNamespace(headers=headers))
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(b"test-secret", response.body)
        self.assertIsNone(await authenticate(connection, SimpleNamespace(
            headers=Headers({"Authorization": "Bearer test-secret"})
        )))

    async def test_executor_sends_key_and_does_not_retry_401(self):
        rejected = InvalidStatus(Response(401, "Unauthorized", Headers()))
        with patch.object(executor, "connect", AsyncMock(side_effect=rejected)) as connect:
            with self.assertRaises(executor.BrokerConnectionError):
                await executor.run_executor("ws://localhost", "test", key="test-secret")
        connect.assert_awaited_once_with(
            "ws://localhost", additional_headers={"Authorization": "Bearer test-secret"}
        )

    async def test_empty_key_fails_closed(self):
        for key in (None, "", "has space", "密钥"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    create_broker(key=key)
                with self.assertRaises(ValueError):
                    await executor.run_executor("ws://localhost", "test", key=key)

    async def test_registration_metadata_does_not_expose_key_or_replace_socket(self):
        from broker.broker import ExecutionBroker

        broker = ExecutionBroker()
        local, remote = MemorySocket(), MemorySocket()
        local.peer, remote.peer = remote, local
        handler = asyncio.create_task(broker.handle_executor(local))
        try:
            await remote.send(json.dumps({
                "type": "register", "executor_id": "test", "key": "must-not-leak",
                "websocket": "forged", "cwd": "/test",
            }))
            await asyncio.wait_for(remote.recv(), timeout=5)
            self.assertNotIn("must-not-leak", str(broker.list_executors()))
            self.assertIs(broker.executors["test"]["websocket"], local)
        finally:
            await remote.close()
            await asyncio.wait_for(handler, timeout=5)


class KeyConfigTests(unittest.TestCase):
    def test_new_configs_have_unique_stable_keys_and_private_files(self):
        with tempfile.TemporaryDirectory() as directory:
            first = ensure_config(str(Path(directory) / "first.yaml"))
            second = ensure_config(str(Path(directory) / "second.yaml"))
            key = load_config(first)["broker"]["key"]
            self.assertGreaterEqual(len(key), 43)
            self.assertNotEqual(key, load_config(second)["broker"]["key"])
            self.assertEqual(ensure_broker_key(first), key)
            self.assertEqual(stat.S_IMODE(os.stat(first).st_mode), 0o600)

    def test_existing_config_migrates_without_losing_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("broker:\n  mcp_port: 9000\nexecutor:\n  broker_url: ws://example:1234\nextra: preserved\n")
            path.chmod(0o644)
            key = ensure_broker_key(str(path))
            self.assertEqual(ensure_broker_key(str(path)), key)
            config = load_config(str(path))
            self.assertEqual(config["broker"]["mcp_port"], 9000)
            self.assertEqual(config["executor"]["broker_url"], "ws://example:1234")
            self.assertIn("extra: preserved", path.read_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_executor_key_sources_and_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            for config, expected in (
                ("broker:\n  key: shared\n", "shared"),
                ("broker:\n  key: local\nexecutor:\n  key: remote\n", "remote"),
            ):
                path.write_text(config)
                with patch.dict(os.environ, {}, clear=True):
                    with patch("broker.cli.run_executor", AsyncMock()) as run:
                        main(["--config", str(path), "executor", "--broker-url", "ws://test", "--executor-id", "test"])
                        self.assertEqual(run.call_args.kwargs["key"], expected)
                with patch.dict(os.environ, {"BROKER_KEY": "environment"}):
                    with patch("broker.cli.run_executor", AsyncMock()) as run:
                        main(["--config", str(path), "executor", "--broker-url", "ws://test", "--executor-id", "test"])
                        self.assertEqual(run.call_args.kwargs["key"], "environment")
            path.write_text("executor:\n  broker_url: ws://test\n")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(SystemExit, "Executor requires BROKER_KEY"):
                    main(["--config", str(path), "executor", "--executor-id", "test"])


if __name__ == "__main__":
    unittest.main()
