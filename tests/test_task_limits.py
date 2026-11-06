import asyncio
import unittest

from mcp.server.mcpserver.exceptions import ToolError

from broker.broker import ExecutionBroker
import test_cancel_task as task_tests
from test_cancel_task import python_command


class TaskLimitTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = task_tests.CancelTaskTests.asyncSetUp
    asyncTearDown = task_tests.CancelTaskTests.asyncTearDown
    wait_until = task_tests.CancelTaskTests.wait_until
    wait_result = task_tests.CancelTaskTests.wait_result

    async def test_capacity_released_after_cancellation(self):
        self.broker.max_pending_tasks = 1
        task = await self.broker.execute("test", python_command("import time; time.sleep(60)"), timeout=0)
        with self.assertRaisesRegex(ToolError, "queue_full"):
            await self.broker.execute("test", "printf should-not-run", timeout=0)
        self.assertEqual(len(self.broker.tasks), 1)
        await self.broker.cancel_task(task["task_id"])
        await self.wait_result(task["task_id"])
        result = await self.broker.execute("test", "exit 0", timeout=5)
        self.assertEqual(result["returncode"], 0)

    async def test_pruning_does_not_break_waiting_execute_or_remove_active_task(self):
        self.broker.max_completed_tasks = 1
        results = await asyncio.gather(*(
            self.broker.execute("test", f"printf {number}", timeout=5) for number in range(5)
        ))
        self.assertEqual([result["returncode"] for result in results], [0] * 5)
        self.assertEqual(len(self.broker.tasks), 1)
        self.assertIn(results[-1]["task_id"], self.broker.tasks)
        active = await self.broker.execute("test", python_command("import time; time.sleep(60)"), timeout=0)
        self.broker._prune_tasks()
        self.assertIn(active["task_id"], self.broker.tasks)
        await self.broker.cancel_task(active["task_id"])
        await self.wait_result(active["task_id"])

    async def test_task_states_timestamps_and_filtered_listing(self):
        first = await self.broker.execute("test", python_command("import time; time.sleep(60)"), timeout=0)
        await self.wait_until(lambda: self.broker.get_task(first["task_id"])["status"] == "running")
        second = await self.broker.execute("test", "exit 0", timeout=0)
        self.assertEqual(second["status"], "submitted")
        waiting = self.broker.get_task(second["task_id"])
        self.assertIsNone(waiting["started_at"])
        self.assertIsNone(waiting["finished_at"])
        self.assertEqual(len(self.broker.list_tasks("test")), 2)
        self.assertEqual(self.broker.list_tasks("other"), [])
        self.assertNotIn("output", self.broker.list_tasks()[0])
        await self.broker.cancel_task(first["task_id"])
        result = await self.wait_result(first["task_id"])
        self.assertLessEqual(result["submitted_at"], result["started_at"])
        self.assertLessEqual(result["started_at"], result["finished_at"])
        await self.wait_result(second["task_id"])
        self.assertEqual(self.broker.list_tasks(), [])
        self.assertEqual(len(self.broker.list_tasks(include_completed=True)), 2)

    def test_invalid_limits(self):
        for name in ("max_completed_tasks", "max_completed_services", "max_pending_tasks"):
            for value in (0, -1, True, "10"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    ExecutionBroker(**{name: value})


if __name__ == "__main__":
    unittest.main()
