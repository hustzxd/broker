import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from mcp.server.mcpserver.exceptions import ToolError

from broker.broker import ExecutionBroker
from broker.cli import main
from broker.config import DEFAULT_CONFIG
from broker.logs import LogBuffer
from test_cancel_task import MemorySocket


class LogBufferTests(unittest.TestCase):
    def test_limit_keeps_latest_bytes_and_streams(self):
        logs = LogBuffer(5)
        logs.append("stdout", "abcd")
        self.assertFalse(logs.read()["truncated"])
        logs.append("stderr", "EFGH")
        self.assertEqual(logs.read(), {
            "entries": [{"stream": "stdout", "data": "d"}, {"stream": "stderr", "data": "EFGH"}],
            "truncated": True,
            "next_cursor": 8,
        })
        self.assertEqual(logs.size, 5)

    def test_multibyte_overflow_preserves_valid_utf8(self):
        logs = LogBuffer(5)
        logs.append("stdout", "你好吗")
        self.assertEqual(logs.read()["entries"], [{"stream": "stdout", "data": "吗"}])
        self.assertEqual(logs.size, 3)
        logs.append("stderr", "ab")
        self.assertEqual(logs.size, 5)
        self.assertTrue(logs.read()["truncated"])
        tiny = LogBuffer(1)
        tiny.append("stdout", "你")
        self.assertEqual(tiny.read(), {"entries": [], "truncated": True, "next_cursor": 3})

    def test_tail_empty_and_large_output(self):
        logs = LogBuffer(8)
        logs.append("stdout", "x" * 100000)
        self.assertEqual(logs.size, 8)
        self.assertEqual(logs.read()["entries"][0]["data"], "x" * 8)
        logs.append("stderr", "")
        self.assertEqual(len(logs.chunks), 1)
        self.assertEqual(logs.read(tail=0)["entries"], [])

    def test_incremental_reads_and_stale_cursor(self):
        logs = LogBuffer(6)
        self.assertEqual(logs.read(cursor=0), {
            "entries": [], "truncated": False, "next_cursor": 0,
        })
        logs.append("stdout", "abc")
        cursor = logs.read()["next_cursor"]
        logs.append("stderr", "defgh")
        result = logs.read(cursor=cursor)
        self.assertEqual(result, {
            "entries": [{"stream": "stderr", "data": "defgh"}],
            "truncated": False, "next_cursor": 8,
        })
        self.assertEqual(logs.read(cursor=8)["entries"], [])
        self.assertFalse(logs.read(cursor=8)["truncated"])
        stale = logs.read(cursor=1)
        self.assertTrue(stale["truncated"])
        self.assertEqual("".join(item["data"] for item in stale["entries"]), "cdefgh")

    def test_cursor_within_chunk_and_tail_precedence(self):
        logs = LogBuffer(20)
        logs.append("stdout", "ab你cd")
        logs.append("stderr", "ef")
        self.assertEqual(logs.read(cursor=2, tail=0)["entries"], [
            {"stream": "stdout", "data": "你cd"}, {"stream": "stderr", "data": "ef"},
        ])
        self.assertTrue(logs.read(tail=1)["truncated"])
        self.assertEqual(logs.read(tail=1)["next_cursor"], 9)
        for cursor in (-1, 10, True, 1.5, 3):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                logs.read(cursor=cursor)


class BrokerLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_ingestion_task_service_and_incremental_queries(self):
        broker = ExecutionBroker(max_log_bytes=5)
        local, remote = MemorySocket(), MemorySocket()
        local.peer, remote.peer = remote, local
        handler = asyncio.create_task(broker.handle_executor(local))
        try:
            await remote.send(json.dumps({"type": "register", "executor_id": "logs"}))
            await asyncio.wait_for(remote.recv(), timeout=5)
            task = await broker.execute("logs", "unused", timeout=0)
            await remote.recv()
            service = await broker.start_service("logs", "logs", "unused")
            await remote.recv()
            task_id, service_id = task["task_id"], service["service_id"]
            for event in (
                {"type": "output", "task_id": task_id, "stream": "stdout", "data": "1234567"},
                {"type": "service_output", "service_id": service_id, "stream": "stderr", "data": "你好abc"},
                {"type": "result", "task_id": task_id, "returncode": 0},
            ):
                await remote.send(json.dumps(event))
            await asyncio.wait_for(broker.tasks[task_id]["done"].wait(), timeout=5)
            task_result = broker.get_task(task_id)
            self.assertEqual(task_result["output"], [{"stream": "stdout", "data": "34567"}])
            self.assertTrue(task_result["truncated"])
            self.assertEqual(task_result["next_cursor"], 7)
            self.assertEqual(broker.get_task(task_id, cursor=7)["output"], [])
            logs = broker.get_service_logs(service_id)
            self.assertEqual(logs, {
                "logs": [{"stream": "stderr", "data": "abc"}],
                "truncated": True, "next_cursor": 9,
            })
            broker.services[service_id]["logs"].append("stdout", "de")
            self.assertEqual(broker.get_service_logs(service_id, tail=0, cursor=9), {
                "logs": [{"stream": "stdout", "data": "de"}],
                "truncated": False, "next_cursor": 11,
            })
            self.assertNotIn("logs", broker.get_service(service_id))
            for query in (
                lambda: broker.get_task(task_id, cursor=8),
                lambda: broker.get_service_logs(service_id, cursor=-1),
            ):
                with self.assertRaisesRegex(ToolError, "invalid_cursor"):
                    query()
            with self.assertRaisesRegex(ToolError, "invalid_tail"):
                broker.get_service_logs(service_id, tail=-1)
        finally:
            await remote.close()
            await asyncio.wait_for(handler, timeout=5)


class LogConfigTests(unittest.TestCase):
    def test_default_validation_and_cli_override(self):
        self.assertEqual(ExecutionBroker().max_log_bytes, DEFAULT_CONFIG["broker"]["max_log_bytes"])
        for value in (0, -1, True, "10"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ExecutionBroker(max_log_bytes=value)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("broker:\n  max_log_bytes: 2048\n")
            with patch("broker.cli.create_broker", return_value=Mock()) as create:
                main(["--config", str(config), "--output-mode", "raw"])
                self.assertEqual(create.call_args.kwargs["max_log_bytes"], 2048)
                main(["--config", str(config), "--output-mode", "raw", "--max-log-bytes", "512"])
                self.assertEqual(create.call_args.kwargs["max_log_bytes"], 512)
                with self.assertRaisesRegex(SystemExit, "positive integer"):
                    main(["--config", str(config), "--max-log-bytes", "0"])
                for value in ("true", "0", "1.5", "invalid"):
                    config.write_text(f"broker:\n  max_log_bytes: {value}\n")
                    with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "positive integer"):
                        main(["--config", str(config)])


if __name__ == "__main__":
    unittest.main()
