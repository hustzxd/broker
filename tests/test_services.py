import asyncio
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock

from mcp.server.mcpserver.exceptions import ToolError

from broker import executor
from broker.broker import ExecutionBroker
import test_cancel_task as task_tests
from test_cancel_task import python_command


class ServiceIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_ids_retry_collisions_and_preserve_existing_services(self):
        broker = ExecutionBroker()
        socket = AsyncMock()
        broker.executors["test"] = {"websocket": socket}
        with patch("broker.broker.request_id", side_effect=[
            "abcdef" + "0" * 26, "abcdef" + "1" * 26, "123456" + "0" * 26,
        ]):
            first = await broker.start_service("test", "first", "exit 0")
            second = await broker.start_service("test", "second", "exit 1")
        self.assertEqual(first["service_id"], "abcdef")
        self.assertEqual(second["service_id"], "123456")
        self.assertEqual(broker.get_service("abcdef")["name"], "first")
        self.assertEqual(broker.get_service("123456")["name"], "second")
        self.assertEqual(socket.send.await_count, 2)

    async def test_existing_long_ids_remain_queryable(self):
        broker = ExecutionBroker()
        service_id = "c6a388277d5b498dbc32deaf8787bd06"
        broker.services[service_id] = {"service_id": service_id, "status": "stopped"}
        self.assertEqual(broker.get_service(service_id)["service_id"], service_id)

    async def test_event_ids_have_six_characters_and_retry_collisions(self):
        broker = ExecutionBroker()
        broker.executors["test"] = {"websocket": AsyncMock()}
        target = {"paseo_server_id": "srv", "paseo_agent_id": "agent"}
        with patch("broker.broker.request_id", side_effect=["111111", "222222"]), patch(
            "broker.broker.uuid.uuid4", side_effect=[
                SimpleNamespace(hex="abcdef" + "0" * 26),
                SimpleNamespace(hex="abcdef" + "1" * 26),
                SimpleNamespace(hex="123456" + "0" * 26),
            ],
        ):
            first = await broker.start_service("test", "first", "exit 0", paseo=target)
            second = await broker.start_service("test", "second", "exit 0", paseo=target)
        self.assertEqual(broker.get_service(first["service_id"])["notification"]["event_id"], "abcdef")
        self.assertEqual(broker.get_service(second["service_id"])["notification"]["event_id"], "123456")


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = task_tests.CancelTaskTests.asyncSetUp
    asyncTearDown = task_tests.CancelTaskTests.asyncTearDown
    wait_until = task_tests.CancelTaskTests.wait_until

    async def test_paseo_notification_is_optional_and_sent_on_terminal_state(self):
        sender = AsyncMock(return_value={"success": True, "deliveryStatus": "queued"})
        self.broker._paseo_sender = sender
        service = await self.broker.start_service(
            "test", "notified", "exit 0",
            paseo={"paseo_server_id": "srv", "paseo_agent_id": "agent"},
        )
        service_id = service["service_id"]
        await self.wait_until(lambda: self.broker.get_service(service_id)["status"] == "stopped")
        await self.wait_until(lambda: self.broker.get_service(service_id)["notification"]["status"] == "queued")
        sender.assert_awaited_once()
        target, prompt = sender.await_args.args
        self.assertEqual(target, {"paseo_server_id": "srv", "paseo_agent_id": "agent"})
        self.assertIn(service_id, prompt)
        event_id = self.broker.get_service(service_id)["notification"]["event_id"]
        self.assertRegex(event_id, r"^[0-9a-f]{6}$")
        self.assertIn(f"event_id={event_id}", prompt)

    async def test_without_paseo_binding_keeps_polling_contract(self):
        sender = AsyncMock()
        self.broker._paseo_sender = sender
        service = await self.broker.start_service("test", "unbound", "exit 0")
        service_id = service["service_id"]
        await self.wait_until(lambda: self.broker.get_service(service_id)["status"] == "stopped")
        self.assertNotIn("notification", self.broker.get_service(service_id))
        sender.assert_not_awaited()

    async def test_paseo_failure_is_recorded_without_changing_service_result(self):
        self.broker._paseo_sender = AsyncMock(side_effect=OSError("daemon unavailable"))
        service = await self.broker.start_service(
            "test", "notify-failure", "exit 3",
            paseo={"paseo_server_id": "srv", "paseo_agent_id": "agent"},
        )
        service_id = service["service_id"]
        await self.wait_until(lambda: self.broker.get_service(service_id)["status"] == "failed")
        await self.wait_until(lambda: self.broker.get_service(service_id)["notification"]["status"] == "failed")
        result = self.broker.get_service(service_id)
        self.assertEqual(result["returncode"], 3)
        self.assertEqual(result["notification"]["error"], "daemon unavailable")

    async def test_service_lifecycle_timestamps(self):
        service = await self.broker.start_service("test", "timestamps", python_command("import time; time.sleep(60)"))
        service_id = service["service_id"]
        await self.wait_until(lambda: self.broker.get_service(service_id)["status"] == "ready")
        ready = self.broker.get_service(service_id)
        self.assertLessEqual(ready["submitted_at"], ready["started_at"])
        self.assertLessEqual(ready["started_at"], ready["ready_at"])
        self.assertIsNone(ready["finished_at"])
        await self.broker.stop_service(service_id)
        await self.wait_until(lambda: self.broker.get_service(service_id)["status"] == "stopped")
        finished = self.broker.get_service(service_id)
        self.assertLessEqual(finished["ready_at"], finished["finished_at"])
        self.assertEqual(finished["started_at"], ready["started_at"])

    async def test_stop_escalates_without_blocking_command_reception(self):
        original_wait_for = asyncio.wait_for
        killpg = executor.os.killpg
        signals = []

        async def short_grace(awaitable, timeout):
            return await original_wait_for(awaitable, timeout=0.05 if timeout == 5 else timeout)

        def record_signal(pid, sig):
            signals.append(sig)
            return killpg(pid, sig)

        with patch.object(executor, "asyncio", SimpleNamespace(**{**vars(asyncio), "wait_for": short_grace})):
            with patch.object(executor.os, "killpg", record_signal):
                service = await self.broker.start_service("test", "ignore-term", python_command(
                    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
                ))
                service_id = service["service_id"]
                await self.wait_until(lambda: bool(self.broker.get_service_logs(service_id)["logs"]))
                await self.broker.stop_service(service_id)
                result = await self.broker.execute("test", "printf responsive", timeout=2)
                self.assertEqual(result["returncode"], 0)
                await self.wait_until(lambda: self.broker.get_service(service_id)["status"] == "stopped")
                self.assertTrue(self.broker.get_service_logs(service_id)["logs"])
                self.assertIn(signal.SIGTERM, signals)
                self.assertIn(signal.SIGKILL, signals)

    async def test_finished_services_retained_and_oldest_evicted(self):
        self.broker.max_completed_services = 1
        first = await self.broker.start_service("test", "finished", "printf last-log")
        await self.wait_until(lambda: self.broker.get_service(first["service_id"])["status"] == "stopped")
        self.assertEqual(self.broker.list_services(), [])
        self.assertEqual(len(self.broker.list_services(include_inactive=True)), 1)
        await self.broker.stop_service(first["service_id"])
        second = await self.broker.start_service("test", "finished", "exit 2")
        await self.wait_until(lambda: self.broker.get_service(second["service_id"])["status"] == "failed")
        self.assertNotIn(first["service_id"], self.broker.services)

    async def test_invalid_parameters_rejected_before_submission(self):
        for options in (
            {"ready_check": {}}, {"ready_check": {"type": "tcp"}},
            {"ready_check": {"type": "http", "port": 80}},
            {"ready_check": {"type": "tcp", "port": 0}},
            {"ready_check": {"type": "tcp", "port": True}},
            {"ready_check": {"type": "tcp", "port": 80, "host": ""}},
            {"startup_timeout": 0}, {"startup_timeout": float("nan")},
        ):
            with self.subTest(options=options), self.assertRaisesRegex(ToolError, "invalid_service"):
                await self.broker.start_service("test", "invalid", "exit 0", **options)
        self.assertEqual(self.broker.services, {})

    async def test_spawn_and_monitor_failures_are_reported_and_connection_stays_usable(self):
        with patch.object(executor.asyncio, "create_subprocess_shell", AsyncMock(side_effect=OSError("spawn failed"))):
            service = await self.broker.start_service("test", "spawn-failure", "unused")
            await self.wait_until(lambda: self.broker.get_service(service["service_id"])["status"] == "failed")
        self.assertIn("spawn failed", self.broker.get_service(service["service_id"])["error"])
        with patch.object(executor, "_wait_for_tcp_ready", AsyncMock(side_effect=OSError("monitor failed"))):
            service = await self.broker.start_service("test", "monitor-failure", python_command("import time; time.sleep(60)"),
                                                    ready_check={"type": "tcp", "port": 12345})
            await self.wait_until(lambda: self.broker.get_service(service["service_id"])["status"] == "failed")
        self.assertIn("monitor failed", self.broker.get_service(service["service_id"])["error"])
        result = await self.broker.execute("test", "exit 0", timeout=2)
        self.assertEqual(result["returncode"], 0)

    async def test_exit_before_readiness_preserves_last_stderr(self):
        service = await self.broker.start_service("test", "early-exit", "printf failure >&2; exit 3",
                                                ready_check={"type": "tcp", "port": 12345})
        await self.wait_until(lambda: self.broker.get_service(service["service_id"])["status"] == "failed")
        result = self.broker.get_service(service["service_id"])
        self.assertEqual(result["returncode"], 3)
        self.assertIn("before becoming ready", result["error"])
        self.assertEqual(self.broker.get_service_logs(service["service_id"])["logs"][0]["data"], "failure")

    async def test_stop_while_starting_and_disconnect_keep_terminal_records(self):
        service = await self.broker.start_service("test", "starting", python_command("import time; print('alive', flush=True); time.sleep(60)"),
                                                ready_check={"type": "tcp", "port": 12345})
        service_id = service["service_id"]
        await self.wait_until(lambda: bool(self.broker.get_service_logs(service_id)["logs"]))
        await self.broker.stop_service(service_id)
        await self.wait_until(lambda: self.broker.get_service(service_id)["status"] == "stopped")
        self.assertNotIn("error", self.broker.get_service(service_id))
        active = await self.broker.start_service("test", "active", python_command("import time; print('alive', flush=True); time.sleep(60)"))
        await self.wait_until(lambda: bool(self.broker.get_service_logs(active["service_id"])["logs"]))
        await self.socket.close()
        await self.wait_until(lambda: self.broker.get_service(active["service_id"])["status"] == "failed")
        self.assertIn("unknown", self.broker.get_service(active["service_id"])["error"])
        self.assertTrue(self.broker.get_service_logs(active["service_id"])["logs"])
        self.assertEqual(self.broker.get_service(service_id)["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
