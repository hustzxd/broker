import asyncio
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from broker.cli import main
from broker.doctor import diagnose
from broker.broker import create_broker
from broker.version import __version__


class DoctorTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_mcp_success_and_wrong_key_through_asgi(self):
        class Listener:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        app = create_broker(key="secret").streamable_http_app(stateless_http=True, json_response=True)
        original_client = httpx.AsyncClient

        def client(**kwargs):
            return original_client(transport=httpx.ASGITransport(app=app), **kwargs)

        with patch("broker.broker.serve", return_value=Listener()):
            async with app.router.lifespan_context(app):
                with patch("broker.doctor.httpx.AsyncClient", client):
                    result = await diagnose("http://127.0.0.1:8273/mcp", "secret")
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["server_version"], __version__)
                    self.assertIn("list_tasks", result["tools"])
                    rejected = await diagnose("http://127.0.0.1:8273/mcp", "wrong")
                    self.assertEqual(rejected["status"], "authentication_failed")

    async def test_classifies_http_network_protocol_and_timeout_errors(self):
        for error, expected in (
            (httpx.HTTPStatusError("secret", request=httpx.Request("POST", "http://test"), response=httpx.Response(401)), "authentication_failed"),
            (httpx.HTTPStatusError("secret", request=httpx.Request("POST", "http://test"), response=httpx.Response(404)), "http_error"),
            (httpx.ConnectError("secret"), "connection_failed"),
            (asyncio.TimeoutError("secret"), "timeout"),
            (ValueError("secret"), "protocol_error"),
        ):
            with self.subTest(expected=expected):
                with patch("broker.doctor.httpx.AsyncClient", side_effect=error):
                    result = await diagnose("http://test/mcp", "secret")
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], expected)
                self.assertNotIn("secret", str(result))


class DoctorCliTests(unittest.TestCase):
    def test_diagnostics_do_not_create_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.yaml"
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(SystemExit, "configuration was not modified"):
                    main(["--config", str(path), "doctor"])
            self.assertFalse(path.exists())

    def test_success_and_failure_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("broker:\n  key: secret\n  mcp_host: 0.0.0.0\n  mcp_port: 9000\n")
            original = path.read_bytes()
            for ok in (True, False):
                with patch.dict(os.environ, {}, clear=True):
                    with patch("broker.cli.diagnose", AsyncMock(return_value={"ok": ok})) as diagnose_mock:
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output):
                            if ok:
                                main(["--config", str(path), "doctor"])
                            else:
                                with self.assertRaises(SystemExit) as raised:
                                    main(["--config", str(path), "doctor"])
                                self.assertEqual(raised.exception.code, 1)
                        diagnose_mock.assert_awaited_once_with("http://127.0.0.1:9000/mcp", "secret")
                        self.assertNotIn("secret", output.getvalue())
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
