import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import yaml

from broker.broker import create_broker
from broker.terminal_output import render_output


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_service_schema_has_no_paseo_argument(self):
        tools = await self.server.list_tools()
        tool = next(tool for tool in tools if tool.name == "start_service")
        self.assertNotIn("paseo", tool.input_schema["properties"])

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
        self.directory = tempfile.TemporaryDirectory()
        self.config_path = Path(self.directory.name) / "config.yaml"
        self.config_path.write_text("broker:\n  key: web-secret\nexecutor:\n  output_mode: raw\n", encoding="utf-8")
        self.server = create_broker(key="web-secret", config_path=str(self.config_path))
        self.broker = self.server.execution_broker
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.streamable_http_app()),
            base_url="http://127.0.0.1:8273",
        )
        self.headers = {"Authorization": "Bearer web-secret"}
        self.socket = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        self.broker.executors["demo"] = {"executor_id": "demo", "cwd": "/workspace", "websocket": self.socket}

    async def asyncTearDown(self):
        await self.broker.shutdown()
        await self.client.aclose()
        self.directory.cleanup()

    async def test_paseo_binding_persists_inherits_and_removes(self):
        path = "/ui/api/executor-paseo"
        target = {"paseo_agent_id": "agent"}
        payload = {"executor_id": "demo", "paseo": target}
        self.assertEqual((await self.client.post(path, json=payload)).status_code, 401)
        self.assertEqual((await self.client.post(path, headers={**self.headers, "Origin": "https://other.example"}, json=payload)).status_code, 403)
        response = await self.client.post(path, headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200)
        state = (await self.client.get("/ui/api/state", headers=self.headers)).json()
        self.assertEqual(state["executor_paseo"]["demo"], target)
        config = yaml.safe_load(self.config_path.read_text())
        self.assertEqual(config["broker"]["key"], "web-secret")
        self.assertEqual(config["executor"]["output_mode"], "raw")
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        restarted = create_broker(key="web-secret", executor_paseo=config["broker"]["executor_paseo"])
        self.assertEqual(restarted.execution_broker.executor_paseo["demo"], target)
        inherited = await self.broker.start_service("demo", "inherited", "unused")
        self.assertEqual(inherited["paseo"], target)
        replacement = {"paseo_agent_id": "different"}
        self.broker.set_executor_paseo("demo", replacement)
        following = await self.broker.start_service("demo", "following", "unused")
        self.assertEqual(following["paseo"], replacement)
        self.assertEqual(self.broker.get_service(inherited["service_id"])["paseo"], target)
        self.broker.set_executor_paseo("demo", target)
        self.assertEqual(self.broker.executor_paseo["demo"], target)
        self.assertNotIn("paseo", self.socket.send.await_args.args[0])
        response = await self.client.post(path, headers=self.headers, json={"executor_id": "demo", "paseo": None})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(yaml.safe_load(self.config_path.read_text())["broker"]["executor_paseo"], {})
        self.assertEqual(self.broker.get_service(inherited["service_id"])["paseo"], target)
        plain = await self.broker.start_service("demo", "plain", "unused")
        self.assertNotIn("notification", plain)

    async def test_paseo_invalid_input_and_failed_write_leave_binding_unchanged(self):
        path = "/ui/api/executor-paseo"
        for payload in ([], {}, {"executor_id": "demo"}, {"executor_id": "demo", "paseo": {}}):
            response = await self.client.post(path, headers=self.headers, json=payload)
            self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.client.post(path, headers=self.headers, content="x" * 8193)).status_code, 413)
        target = {"paseo_agent_id": "agent"}
        with patch("broker.config.os.replace", side_effect=OSError("read only")):
            response = await self.client.post(path, headers=self.headers, json={"executor_id": "demo", "paseo": target})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.broker.executor_paseo, {})
        self.assertNotIn("executor_paseo", yaml.safe_load(self.config_path.read_text())["broker"])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.config_path])

    async def test_paseo_test_message_uses_saved_binding(self):
        path = "/ui/api/executor-paseo/test"
        payload = {"executor_id": "demo"}
        self.assertEqual((await self.client.post(path, headers=self.headers, json=payload)).status_code, 400)
        target = {"paseo_agent_id": "agent"}
        self.broker.set_executor_paseo("demo", target)
        self.broker._paseo_sender = AsyncMock(return_value={"status": "sent"})
        gate = asyncio.Event()
        async def wait_for_gate(target):
            await gate.wait()
        self.broker._paseo_waiter = wait_for_gate
        self.assertEqual((await self.client.post(path, json=payload)).status_code, 401)
        response = await self.client.post(path, headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 202)
        self.assertIn(response.json()["status"], {"queued", "waiting_idle"})
        await asyncio.sleep(0)
        self.broker._paseo_sender.assert_not_awaited()
        gate.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if self.broker.paseo_tests["demo"]["status"] == "submitted":
                break
        self.broker._paseo_sender.assert_awaited_once()
        self.assertEqual(self.broker._paseo_sender.await_args.args[0], target)
        self.broker._paseo_sender.side_effect = OSError("daemon unavailable")
        self.assertEqual((await self.client.post(path, headers=self.headers, json=payload)).status_code, 202)
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(self.broker.paseo_tests["demo"]["status"], "failed")

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
