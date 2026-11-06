import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx

from broker.broker import create_broker


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = create_broker(key="web-secret")
        self.broker = self.server.execution_broker
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.streamable_http_app()),
            base_url="http://127.0.0.1:8273",
        )
        self.headers = {"Authorization": "Bearer web-secret"}
        self.socket = SimpleNamespace(send=AsyncMock())
        self.broker.executors["demo"] = {"executor_id": "demo", "cwd": "/workspace", "websocket": self.socket}

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_only_public_login_assets_bypass_auth(self):
        for path in ("/ui/", "/ui/app.js", "/ui/style.css"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn("web-secret", response.text)
        for path in ("/mcp", "/ui/api/state", "/ui/api/tasks/missing/logs", "/ui/unknown.js"):
            for headers in ({}, {"Authorization": "Bearer incorrect"}):
                response = await self.client.get(path, headers=headers)
                self.assertEqual(response.status_code, 401)
        response = await self.client.post("/ui/api/tasks/missing/stop")
        self.assertEqual(response.status_code, 401)
        response = await self.client.get("/ui/api/state", headers={**self.headers, "Origin": "https://untrusted.example"})
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    async def test_state_logs_progress_revision_and_controls(self):
        task = await self.broker.execute("demo", "printf hello", timeout=0)
        service = await self.broker.start_service("demo", "web-test", "serve")
        task_id, service_id = task["task_id"], service["service_id"]
        self.broker.tasks[task_id]["output"].append("stderr", "\rold\rnew\x1b[K\n")
        response = await self.client.get("/ui/api/state", headers=self.headers)
        state = response.json()
        self.assertEqual(state["executors"][0]["executor_id"], "demo")
        self.assertEqual(state["tasks"][0]["task_id"], task_id)
        self.assertNotIn("websocket", response.text)
        self.assertNotIn("web-secret", response.text)
        self.assertNotIn("output", state["tasks"][0])
        response = await self.client.get(f"/ui/api/tasks/{task_id}/logs", headers=self.headers)
        self.assertEqual(response.json()["text"], "new\n")
        revision = response.json()["revision"]
        response = await self.client.get(f"/ui/api/tasks/{task_id}/logs?revision={revision}", headers=self.headers)
        self.assertTrue(response.json()["unchanged"])
        response = await self.client.post(f"/ui/api/tasks/{task_id}/stop", headers=self.headers)
        self.assertEqual(response.json()["status"], "cancelling")
        response = await self.client.post(f"/ui/api/services/{service_id}/stop", headers=self.headers)
        self.assertEqual(response.json()["status"], "stopping")
        response = await self.client.get("/ui/api/tasks/missing/logs", headers=self.headers)
        self.assertEqual(response.status_code, 404)

    async def test_controls_reject_cross_origin_and_do_not_serve_arbitrary_files(self):
        response = await self.client.post("/ui/api/tasks/test/stop", headers={**self.headers, "Origin": "https://untrusted.example"})
        self.assertEqual(response.status_code, 403)
        self.socket.send.assert_not_awaited()
        response = await self.client.post("/ui/api/tasks/missing/stop", headers=self.headers)
        self.assertEqual(response.status_code, 400)
        response = await self.client.get("/ui/config.py", headers=self.headers)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
