import asyncio
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from mcp.server.mcpserver.exceptions import ToolError

from broker.broker import ExecutionBroker
from broker import executor


def python_command(code):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


class MemorySocket:
    """Exercise both message loops without opening a network listener."""

    def __init__(self):
        self.messages = asyncio.Queue()
        self.peer = None

    async def send(self, payload):
        await self.peer.messages.put(payload)

    async def recv(self):
        return await self.messages.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        payload = await self.recv()
        if payload is None:
            raise StopAsyncIteration
        return payload

    async def close(self):
        await self.messages.put(None)
        await self.peer.messages.put(None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


class CancelTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.broker = ExecutionBroker()
        self.socket = MemorySocket()
        remote = MemorySocket()
        self.socket.peer = remote
        remote.peer = self.socket
        self.connection_patch = patch.object(executor, "connect", AsyncMock(return_value=remote))
        self.connection_patch.start()
        self.broker_loop = asyncio.create_task(self.broker.handle_executor(self.socket))
        self.executor_loop = asyncio.create_task(executor.run_executor("ws://test", "test", key="test-key"))
        await self.wait_until(lambda: "test" in self.broker.executors)

    async def asyncTearDown(self):
        self.executor_loop.cancel()
        await self.socket.close()
        try:
            await asyncio.wait_for(
                asyncio.gather(self.executor_loop, self.broker_loop, return_exceptions=True), timeout=5
            )
        finally:
            self.connection_patch.stop()

    async def wait_until(self, condition):
        async def poll():
            while not condition():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), timeout=5)

    async def wait_result(self, task_id):
        await asyncio.wait_for(self.broker.tasks[task_id]["done"].wait(), timeout=5)
        return self.broker.get_task(task_id)

    async def test_cancel_kills_shell_and_descendants_and_preserves_output(self):
        # The child inherits the output pipes and ignores SIGTERM. A result can
        # arrive only after the whole group has stopped and those pipes close.
        child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('child ready', flush=True); time.sleep(60)"
        code = (
            "import subprocess,sys; "
            f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
            "child.wait()"
        )
        submitted = await self.broker.execute("test", python_command(code), timeout=0)
        task_id = submitted["task_id"]
        await self.wait_until(lambda: any(
            "child ready" in output["data"]
            for output in self.broker.get_task(task_id)["output"]
        ))
        response = await self.broker.cancel_task(task_id)
        self.assertIn(response["status"], {"cancelling", "cancelled"})
        await self.broker.cancel_task(task_id)
        result = await self.wait_result(task_id)
        self.assertEqual(result["status"], "cancelled")
        self.assertNotEqual(result["returncode"], 0)
        self.assertIn("child ready", "".join(item["data"] for item in result["output"]))
        self.assertEqual(await self.broker.cancel_task(task_id), result)
        following = await self.broker.execute("test", "printf usable", timeout=5)
        self.assertEqual(following["status"], "completed")
        self.assertEqual(following["returncode"], 0)

    async def test_cancel_queued_command_does_not_launch_it(self):
        first = await self.broker.execute("test", python_command("import time; time.sleep(60)"), timeout=0)
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-exist"
            second = await self.broker.execute(
                "test", python_command(f"from pathlib import Path; Path({str(marker)!r}).touch()"), timeout=0
            )
            await self.broker.cancel_task(second["task_id"])
            self.assertEqual((await self.wait_result(second["task_id"]))["status"], "cancelled")
            self.assertFalse(marker.exists())
            self.assertFalse(self.broker.tasks[first["task_id"]]["done"].is_set())
            await self.broker.cancel_task(first["task_id"])
            await self.wait_result(first["task_id"])

    async def test_commands_remain_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "first-finished"
            first = await self.broker.execute(
                "test", python_command(f"import time; from pathlib import Path; time.sleep(0.1); Path({str(marker)!r}).touch()"), timeout=0
            )
            second = await self.broker.execute(
                "test", python_command(f"from pathlib import Path; assert Path({str(marker)!r}).exists()"), timeout=0
            )
            self.assertEqual((await self.wait_result(first["task_id"]))["returncode"], 0)
            self.assertEqual((await self.wait_result(second["task_id"]))["returncode"], 0)

    async def test_cancel_during_process_creation(self):
        spawning = asyncio.Event()
        release = asyncio.Event()
        create_process = asyncio.create_subprocess_shell

        async def delayed_spawn(*args, **kwargs):
            spawning.set()
            await release.wait()
            return await create_process(*args, **kwargs)

        with patch.object(executor.asyncio, "create_subprocess_shell", delayed_spawn):
            submitted = await self.broker.execute("test", python_command("import time; time.sleep(60)"), timeout=0)
            await asyncio.wait_for(spawning.wait(), timeout=5)
            await self.broker.cancel_task(submitted["task_id"])
            # Ensure the cancellation message is handled while spawn is pending.
            await self.wait_until(lambda: self.socket.peer.messages.empty())
            release.set()
            result = await self.wait_result(submitted["task_id"])
            self.assertEqual(result["status"], "cancelled")

    async def test_completed_unknown_and_offline_tasks(self):
        result = await self.broker.execute("test", "exit 7", timeout=5)
        self.assertEqual(result["returncode"], 7)
        self.assertEqual(await self.broker.cancel_task(result["task_id"]), result)
        with self.assertRaisesRegex(ToolError, "unknown_task"):
            await self.broker.cancel_task("missing")
        self.broker.tasks["offline"] = {
            "executor_id": "offline", "status": "submitted", "done": asyncio.Event(),
        }
        with self.assertRaisesRegex(ToolError, "executor_offline"):
            await self.broker.cancel_task("offline")

    async def test_cancel_releases_execute_waiter(self):
        pending = asyncio.create_task(self.broker.execute(
            "test", python_command("import time; time.sleep(60)"), timeout=30
        ))
        await self.wait_until(lambda: bool(self.broker.tasks))
        task_id = next(iter(self.broker.tasks))
        await self.broker.cancel_task(task_id)
        result = await asyncio.wait_for(pending, timeout=5)
        self.assertEqual(result["status"], "cancelled")

    async def test_failed_cancel_send_restores_status(self):
        submitted = await self.broker.execute("test", python_command("import time; time.sleep(60)"), timeout=0)
        task_id = submitted["task_id"]
        previous_status = self.broker.get_task(task_id)["status"]
        with patch.object(self.socket, "send", AsyncMock(side_effect=OSError("send failed"))):
            with self.assertRaisesRegex(OSError, "send failed"):
                await self.broker.cancel_task(task_id)
        self.assertEqual(self.broker.get_task(task_id)["status"], previous_status)
        await self.broker.cancel_task(task_id)
        self.assertEqual((await self.wait_result(task_id))["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
