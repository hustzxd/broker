import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from broker.broker import ExecutionBroker
from broker.paseo import send_completion, wait_until_idle


class PaseoTests(unittest.IsolatedAsyncioTestCase):
    async def test_unbinding_cancels_only_own_unsent_notifications(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("broker: {}\n", encoding="utf-8")
            gate = asyncio.Event()
            waiting = asyncio.Event()
            sent = []

            async def waiter(target):
                waiting.set()
                await gate.wait()

            async def sender(target, prompt):
                sent.append(prompt)
                return {"status": "sent"}

            broker = ExecutionBroker(config_path=str(config), paseo_waiter=waiter, paseo_sender=sender)
            target = {"paseo_agent_id": "shared"}
            broker.set_executor_paseo("first", target)
            broker.set_executor_paseo("second", target)
            socket = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
            broker.executors = {name: {"websocket": socket} for name in ("first", "second")}
            service = await broker.start_service("first", "running", "serve")
            pending = broker.services[service["service_id"]]["notification"]
            broker.enqueue_paseo_test("first")
            await asyncio.wait_for(waiting.wait(), timeout=2)
            first_test = broker.paseo_tests["first"]
            finished = await broker.start_service("first", "finished", "serve")
            finished_record = broker.services[finished["service_id"]]
            finished_record.update(status="stopped", finished_at=1)
            broker._schedule_service_notification(finished_record)
            broker.services.pop(finished["service_id"])
            broker.enqueue_paseo_test("second")
            second_test = broker.paseo_tests["second"]
            self.assertEqual(first_test["status"], "waiting_idle")
            self.assertEqual(finished_record["notification"]["status"], "queued")
            self.assertEqual(second_test["status"], "queued")
            broker.set_executor_paseo("first", None)
            self.assertEqual(first_test["status"], "cancelled")
            self.assertEqual(pending["status"], "cancelled")
            self.assertEqual(finished_record["notification"]["status"], "cancelled")
            broker._schedule_service_notification(broker.services[service["service_id"]])
            await asyncio.sleep(0)
            self.assertFalse(sent)
            gate.set()
            await asyncio.gather(*list(broker._notification_tasks))
            self.assertEqual(second_test["status"], "submitted")
            self.assertEqual(len(sent), 1)
            self.assertIn("executor=second", sent[0])
            broker.set_executor_paseo("first", target)
            self.assertEqual(pending["status"], "cancelled")
            await broker.shutdown()

    async def test_unbinding_preserves_already_sending_status(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("broker: {}\n", encoding="utf-8")
            sending = asyncio.Event()
            finish = asyncio.Event()

            async def sender(target, prompt):
                sending.set()
                await finish.wait()
                return {"status": "sent"}

            broker = ExecutionBroker(config_path=str(config), paseo_waiter=AsyncMock(), paseo_sender=sender)
            broker.set_executor_paseo("first", {"paseo_agent_id": "agent"})
            broker.enqueue_paseo_test("first")
            await asyncio.wait_for(sending.wait(), timeout=2)
            broker.set_executor_paseo("first", None)
            self.assertEqual(broker.paseo_tests["first"]["status"], "sending")
            finish.set()
            await asyncio.gather(*list(broker._notification_tasks))
            self.assertEqual(broker.paseo_tests["first"]["status"], "submitted")
            await broker.shutdown()

    async def test_wait_retries_busy_and_permission_without_sending(self):
        with patch("broker.paseo._run_cli", new_callable=AsyncMock) as run, patch(
            "broker.paseo.asyncio.sleep", new_callable=AsyncMock,
        ):
            run.side_effect = [{"status": "timeout"}, {"status": "permission"}, {"status": "idle"}]
            await wait_until_idle({"paseo_agent_id": "agent"})
            self.assertEqual(run.await_count, 3)
            self.assertEqual(run.await_args.args[1][0], "wait")

    async def test_unavailable_agent_is_not_treated_as_idle(self):
        with patch("broker.paseo._run_cli", new_callable=AsyncMock, return_value={"status": "error"}):
            with self.assertRaises(RuntimeError):
                await wait_until_idle({"paseo_agent_id": "agent"})

    async def test_send_uses_no_wait_and_timeout_cleans_up_process(self):
        process = Mock(returncode=0)
        process.communicate = AsyncMock(return_value=(b'{"status":"sent"}', b""))
        with patch("broker.paseo.shutil.which", return_value="/bin/paseo"), patch(
            "broker.paseo.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=process,
        ) as spawn:
            await send_completion({"paseo_agent_id": "agent"}, "result")
            self.assertIn("--no-wait", spawn.await_args.args)
            process.returncode = None
            process.communicate.side_effect = [asyncio.TimeoutError(), (b"", b"")]
            with self.assertRaises(TimeoutError):
                await send_completion({"paseo_agent_id": "agent"}, "result")
            process.kill.assert_called_once()

    async def test_per_agent_fifo_other_agent_progress_and_shutdown(self):
        gate = asyncio.Event()
        waiting = asyncio.Event()
        received = []

        async def waiter(target):
            if target["paseo_agent_id"] == "busy":
                waiting.set()
                await gate.wait()

        async def sender(target, prompt):
            received.append(prompt)
            return {"status": "sent"}

        broker = ExecutionBroker(paseo_waiter=waiter, paseo_sender=sender)
        first, second, other = {}, {}, {}
        broker._enqueue_notification("first", {"paseo_agent_id": "busy"}, "first", first)
        broker._enqueue_notification("second", {"paseo_agent_id": "busy"}, "second", second)
        broker._enqueue_notification("other", {"paseo_agent_id": "idle"}, "other", other)
        await waiting.wait()
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(received, ["other"])
        self.assertEqual(first["status"], "waiting_idle")
        self.assertEqual(second["status"], "queued")
        gate.set()
        await asyncio.gather(*list(broker._notification_tasks))
        self.assertEqual(received, ["other", "first", "second"])
        self.assertEqual(first["status"], "submitted")
        gate.clear()
        pending = {}
        broker._enqueue_notification("first", {"paseo_agent_id": "busy"}, "cancel", pending)
        await asyncio.sleep(0)
        await broker.shutdown()
        self.assertEqual(pending["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
