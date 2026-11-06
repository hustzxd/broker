import asyncio
import json
import os
from pathlib import Path
import tempfile
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from websockets.exceptions import InvalidURI

from broker import executor
from broker.broker import ExecutionBroker
from test_cancel_task import MemorySocket, python_command


def executor_sleep_patch(sleep):
    # Replace only the executor's sleep, keeping test polling and broker timing real.
    return patch.object(executor, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep}))


async def wait_until(condition):
    async def poll():
        while not condition():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), timeout=5)


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_backoff_cap_and_cancel_during_retry(self):
        delays = []

        async def retry_sleep(delay):
            delays.append(delay)
            if len(delays) == 8:
                raise asyncio.CancelledError

        with patch.object(executor, "connect", AsyncMock(side_effect=OSError("offline"))) as connect:
            with executor_sleep_patch(retry_sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await executor.run_executor("ws://test", "test", key="test-key")
        self.assertEqual(delays, [1, 2, 4, 8, 16, 30, 30, 30])
        self.assertEqual(connect.await_count, 8)

    async def test_stable_registration_resets_backoff(self):
        delays = []
        sessions = 0

        async def session(socket, url, state, metadata, interactive):
            nonlocal sessions
            sessions += 1
            state["registered"] = True
            state["registered_at"] = asyncio.get_running_loop().time() - (31 if sessions == 3 else 0)

        async def retry_sleep(delay):
            delays.append(delay)
            if len(delays) == 4:
                raise asyncio.CancelledError

        with patch.object(executor, "connect", AsyncMock(return_value=object())):
            with patch.object(executor, "_run_executor_connection", session), executor_sleep_patch(retry_sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await executor.run_executor("ws://test", "test", key="test-key")
        self.assertEqual(delays, [1, 2, 1, 2])

    async def test_invalid_url_does_not_retry(self):
        with patch.object(executor, "connect", AsyncMock(side_effect=InvalidURI("bad", "invalid"))) as connect:
            with self.assertRaises(executor.BrokerConnectionError):
                await executor.run_executor("bad", "test", key="test-key")
        self.assertEqual(connect.await_count, 1)

    async def test_reconnect_cleans_old_work_and_accepts_new_commands(self):
        broker = ExecutionBroker()
        broker_loops = []
        sockets = []
        delays = []

        async def connect(url, **kwargs):
            local, remote = MemorySocket(), MemorySocket()
            local.peer, remote.peer = remote, local
            sockets.append(local)
            broker_loops.append(asyncio.create_task(broker.handle_executor(local)))
            return remote

        async def retry_sleep(delay):
            delays.append(delay)
            await asyncio.sleep(0)

        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "queued-must-not-run"
            with patch.object(executor, "connect", connect), executor_sleep_patch(retry_sleep):
                runner = asyncio.create_task(executor.run_executor("ws://test", "test", key="test-key"))
                pid = None
                try:
                    await wait_until(lambda: "test" in broker.executors)
                    completed = await broker.execute("test", "printf completed", timeout=5)
                    code = "import os,time; print(os.getpid(), flush=True); time.sleep(60)"
                    pending = asyncio.create_task(broker.execute("test", python_command(code), timeout=60))
                    await wait_until(lambda: any(
                        task["command"] == python_command(code) and task["output"].size
                        for task in broker.tasks.values()
                    ))
                    running_id = next(task_id for task_id, task in broker.tasks.items() if task["command"] == python_command(code))
                    pid = int(broker.get_task(running_id)["output"][0]["data"].strip())
                    queued = await broker.execute(
                        "test", python_command(f"from pathlib import Path; Path({str(marker)!r}).touch()"), timeout=0
                    )
                    await sockets[0].close()
                    result = await asyncio.wait_for(pending, timeout=5)
                    self.assertEqual(result["status"], "lost")
                    self.assertIsNone(result["returncode"])
                    self.assertIn("unknown", result["error"])
                    self.assertTrue(result["output"])
                    self.assertEqual(broker.get_task(queued["task_id"])["status"], "lost")
                    await wait_until(lambda: "test" in broker.executors and broker.executors["test"]["websocket"] is not sockets[0])
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                    self.assertFalse(marker.exists())
                    self.assertEqual(broker.get_task(completed["task_id"]), completed)
                    following = await broker.execute("test", "printf reconnected", timeout=5)
                    self.assertEqual(following["status"], "completed")
                    self.assertEqual(following["output"][0]["data"], "reconnected")
                    self.assertEqual(broker.get_task(running_id)["status"], "lost")
                    self.assertEqual(delays, [1])
                finally:
                    runner.cancel()
                    await asyncio.gather(runner, return_exceptions=True)
                    for socket in sockets:
                        await socket.close()
                    await asyncio.wait_for(asyncio.gather(*broker_loops), timeout=5)

    async def test_registration_disconnect_and_duplicate_retry_keep_name(self):
        attempts = 0
        received_ids = []
        peers = []
        delays = []

        async def peer(socket, attempt):
            registration = json.loads(await socket.recv())
            received_ids.append(registration["executor_id"])
            if attempt == 1:
                # Drop before the registration reply.
                raise OSError("registration connection lost")
            await socket.send(json.dumps({"accepted": attempt != 3, "reason": "name in use"}))
            if attempt == 2:
                await socket.close()

        async def connect(url, **kwargs):
            nonlocal attempts
            attempts += 1
            local, remote = MemorySocket(), MemorySocket()
            local.peer, remote.peer = remote, local
            if attempts == 1:
                async def failed_recv():
                    raise OSError("registration connection lost")
                remote.recv = failed_recv
            peers.append(asyncio.create_task(peer(local, attempts)))
            return remote

        async def retry_sleep(delay):
            delays.append(delay)
            if len(delays) == 3:
                raise asyncio.CancelledError
            await asyncio.sleep(0)

        with patch.object(executor, "connect", connect), executor_sleep_patch(retry_sleep):
            with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                with self.assertRaises(asyncio.CancelledError):
                    await executor.run_executor("ws://test", "same-name", interactive=True, key="test-key")
        await asyncio.gather(*peers, return_exceptions=True)
        self.assertEqual(received_ids, ["same-name"] * 3)
        self.assertEqual(delays, [1, 2, 4])

    async def test_send_failure_marks_task_lost(self):
        broker = ExecutionBroker()
        socket = SimpleNamespace(send=AsyncMock(side_effect=OSError("offline")))
        broker.executors["test"] = {"websocket": socket}
        result = await broker.execute("test", "unused")
        self.assertEqual(result["status"], "lost")
        self.assertTrue(broker.tasks[result["task_id"]]["done"].is_set())

    async def test_service_cleanup_escalates_after_grace_period(self):
        process = await asyncio.create_subprocess_shell(
            python_command("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"),
            stdout=asyncio.subprocess.PIPE, start_new_session=True,
        )
        original_wait_for = asyncio.wait_for

        async def shorter_grace(awaitable, timeout):
            self.assertEqual(timeout, 5)
            return await original_wait_for(awaitable, timeout=0.05)

        try:
            self.assertEqual(await original_wait_for(process.stdout.readline(), timeout=5), b"ready\n")
            with patch.object(executor, "asyncio", SimpleNamespace(**{**vars(asyncio), "wait_for": shorter_grace})):
                self.assertEqual(await executor._terminate_services({"test": process}), 1)
            self.assertIsNotNone(process.returncode)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await original_wait_for(process.wait(), timeout=5)


if __name__ == "__main__":
    unittest.main()
