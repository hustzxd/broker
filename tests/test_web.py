import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from broker.broker import create_broker
from broker.terminal_output import render_output


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_history_cleanup_is_authenticated_and_rejects_reconnect(self):
        task = await self.broker.execute("demo", "unused", timeout=0)
        service = await self.broker.start_service("demo", "history", "unused")
        task_id, service_id = task["task_id"], service["service_id"]
        payload = {"executor_id": "demo"}
        path = "/ui/api/history/clear"
        self.assertEqual((await self.client.post(path, json=payload)).status_code, 401)
        self.assertEqual((await self.client.post(path, headers=self.headers, json=payload)).status_code, 409)
        connection = self.broker.executors.pop("demo")
        self.assertEqual((await self.client.post(path, headers=self.headers, json=payload)).status_code, 409)
        self.broker._mark_task_lost(self.broker.tasks[task_id])
        self.broker.services[service_id].update(status="failed", finished_at=1)
        self.broker.executors["demo"] = connection
        self.assertEqual((await self.client.post(path, headers=self.headers, json=payload)).status_code, 409)
        self.broker.executors.pop("demo")
        self.assertEqual((await self.client.post(path, headers={**self.headers, "Origin": "https://other.example"}, json=payload)).status_code, 403)
        response = await self.client.post(path, headers=self.headers, json=payload)
        self.assertEqual(response.json(), {"tasks_deleted": 1, "services_deleted": 1})
        self.assertNotIn(task_id, self.broker.tasks)
        self.assertNotIn(service_id, self.broker.services)
        self.assertEqual((await self.client.post(path, headers=self.headers, content="invalid")).status_code, 400)

    async def test_render_cache_reused_and_invalidated_on_output_change(self):
        task = await self.broker.execute("demo", "unused", timeout=0)
        task_id = task["task_id"]
        buffer = self.broker.tasks[task_id]["output"]
        buffer.append("stdout", "old\rnew\x1b[K")
        path = f"/ui/api/tasks/{task_id}/logs"
        with patch("broker.web.render_output", wraps=render_output) as renderer:
            first = await self.client.get(path, headers=self.headers)
            second = await self.client.get(path, headers=self.headers)
            self.assertEqual(first.json(), second.json())
            self.assertEqual(renderer.call_count, 1)
            buffer.append("stdout", " output")
            updated = await self.client.get(path, headers=self.headers)
            self.assertEqual(updated.json()["text"], "new output")
            self.assertEqual(renderer.call_count, 2)
            del self.broker.tasks[task_id]
            missing = await self.client.get(path, headers=self.headers)
            self.assertEqual(missing.status_code, 404)

    async def test_render_cache_evicts_old_entries(self):
        self.broker.max_pending_tasks = 40
        task_ids = [(await self.broker.execute("demo", "unused", timeout=0))["task_id"] for _ in range(33)]
        with patch("broker.web.render_output", wraps=render_output) as renderer:
            for task_id in task_ids:
                self.broker.tasks[task_id]["output"].append("stdout", "log")
                await self.client.get(f"/ui/api/tasks/{task_id}/logs", headers=self.headers)
            self.assertEqual(renderer.call_count, 33)
            await self.client.get(f"/ui/api/tasks/{task_ids[-1]}/logs", headers=self.headers)
            self.assertEqual(renderer.call_count, 33)
            await self.client.get(f"/ui/api/tasks/{task_ids[0]}/logs", headers=self.headers)
            self.assertEqual(renderer.call_count, 34)

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
        for path in ("/ui/", "/ui/app.js", "/ui/style.css", "/ui/favicon.svg"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn("web-secret", response.text)
            if path.endswith("favicon.svg"):
                self.assertIn("image/svg+xml", response.headers["content-type"])
                self.assertIn("img-src 'self'", response.headers["Content-Security-Policy"])
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
