import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from broker import executor
from broker.cli import main
from test_cancel_task import MemorySocket
from broker.version import __version__
from test_cancel_task import python_command


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.errors))
        self.stack.enter_context(patch.object(executor, "_partial_output_stream", None))
        self.stack.enter_context(patch.object(executor, "_partial_output_source", None))
        self.stack.enter_context(patch.object(executor, "_COLOR", True))

    def tearDown(self):
        self.stack.close()

    async def test_partial_output_gets_local_newline_without_changing_wire_data(self):
        socket = AsyncMock()
        for stream, command, destination in (
            ("stdout", "printf partial", self.output),
            ("stderr", "printf partial >&2", self.errors),
        ):
            with self.subTest(stream=stream):
                self.output.seek(0)
                self.output.truncate()
                self.errors.seek(0)
                self.errors.truncate()
                socket.reset_mock()
                await executor._execute(socket, {"task_id": "test", "command": command},
                                        {"process": None, "cancel_requested": False})
                events = [json.loads(call.args[0]) for call in socket.send.await_args_list]
                captured = [event for event in events if event["type"] == "output"]
                self.assertEqual("".join(event["data"] for event in captured), "partial")
                self.assertTrue(all(event["stream"] == stream for event in captured))
                self.assertIn("partial\n", destination.getvalue())
                self.assertIn("exit 0", self.output.getvalue())
                if stream == "stderr":
                    self.assertEqual(self.errors.getvalue(), "partial\n")

    async def test_existing_newline_and_original_ansi_are_preserved(self):
        socket = AsyncMock()
        text = "\033[34mnormal log\033[0m\n"
        command = python_command(f"import sys; sys.stderr.write({text!r})")
        await executor._execute(socket, {"task_id": "test", "command": command},
                                {"process": None, "cancel_requested": False})
        self.assertEqual(self.errors.getvalue(), text)
        events = [json.loads(call.args[0]) for call in socket.send.await_args_list]
        self.assertEqual("".join(event["data"] for event in events if event["type"] == "output"), text)

    async def test_cancellation_displays_reason_and_keeps_result(self):
        socket = AsyncMock()
        task = {"process": None, "cancel_requested": False}
        runner = asyncio.create_task(executor._execute(socket, {
            "task_id": "cancel-test", "command": python_command("import time; time.sleep(60)"),
        }, task))
        try:
            async def wait_for_spawn():
                while task["process"] is None:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_for_spawn(), timeout=5)
            executor._cancel_task(task, reason="connection closed or executor shutting down")
            await asyncio.wait_for(runner, timeout=5)
            self.assertIn("CANCELLED", self.output.getvalue())
            self.assertIn("connection closed or executor shutting down", self.output.getvalue())
            events = [json.loads(call.args[0]) for call in socket.send.await_args_list]
            self.assertEqual(events[-1]["status"], "cancelled")
        finally:
            executor._cancel_task(task)
            await asyncio.gather(runner, return_exceptions=True)

    async def test_service_startup_and_monitor_errors_are_visible(self):
        socket = AsyncMock()
        with patch.object(executor.asyncio, "create_subprocess_shell", AsyncMock(side_effect=OSError("spawn failed"))):
            await executor._start_service(socket, {"service_id": "test", "name": "demo", "command": "unused"}, {})
        self.assertIn("service demo failed", self.output.getvalue())
        self.assertIn("reason: Service startup failed: spawn failed", self.output.getvalue())
        services, monitors = {}, set()
        with patch.object(executor, "_wait_for_tcp_ready", AsyncMock(side_effect=OSError("monitor failed"))):
            await executor._start_service(socket, {
                "service_id": "monitor", "name": "monitored", "command": python_command("import time; time.sleep(60)"),
                "ready_check": {"type": "tcp", "port": 12345},
            }, services, monitors)
            await asyncio.wait_for(asyncio.gather(*monitors), timeout=5)
        self.assertIn("service monitored failed", self.output.getvalue())
        self.assertIn("reason: Service monitoring failed: monitor failed", self.output.getvalue())

    def test_prefixes_split_lines_source_switches_and_progress_updates(self):
        token = executor._OUTPUT_MODE.set("prefixed")
        try:
            first = executor._OutputWriter("task abcd1234", "stdout")
            second = executor._OutputWriter("svc vllm", "stdout")
            first.write("hel")
            first.write("lo\nnext")
            second.write("service\n")
            first.write("continued\rprogress\r")
            first.write("\nfinished\n")
            self.assertEqual(self.output.getvalue(), (
                "[task abcd1234 stdout] hello\n"
                "[task abcd1234 stdout] next\n"
                "[svc vllm stdout] service\n"
                "[task abcd1234 stdout] continued\n"
                "[task abcd1234 stdout] progress\n"
                "[task abcd1234 stdout] finished\n"
            ))
        finally:
            executor._OUTPUT_MODE.reset(token)

    async def test_prefixed_task_and_service_output_do_not_change_wire_data(self):
        token = executor._OUTPUT_MODE.set("prefixed")
        try:
            socket = AsyncMock()
            await executor._execute(socket, {"task_id": "abcdefgh1234", "command": "printf task-output >&2"},
                                    {"process": None, "cancel_requested": False})
            self.assertEqual(self.errors.getvalue(), "[task abcdefgh stderr] task-output\n")
            stream = asyncio.StreamReader()
            stream.feed_data(b"service\rupdate\n")
            stream.feed_eof()
            await executor._forward_service_output(socket, "service-id", stream, "stdout", "vllm")
            self.assertIn("[svc vllm stdout] service\n[svc vllm stdout] update\n", self.output.getvalue())
            events = [json.loads(call.args[0]) for call in socket.send.await_args_list]
            self.assertEqual(next(event["data"] for event in events if event["type"] == "output"), "task-output")
            self.assertEqual(next(event["data"] for event in events if event["type"] == "service_output"), "service\rupdate\n")
        finally:
            executor._OUTPUT_MODE.reset(token)

    def test_status_interrupting_partial_line_repeats_prefix(self):
        token = executor._OUTPUT_MODE.set("prefixed")
        try:
            writer = executor._OutputWriter("task 12345678", "stdout")
            writer.write("partial")
            executor._emit("QUEUED other")
            writer.write("rest\n")
            self.assertEqual(self.output.getvalue(), "[task 12345678 stdout] partial\nQUEUED other\n[task 12345678 stdout] rest\n")
        finally:
            executor._OUTPUT_MODE.reset(token)

    async def test_queue_start_wait_time_and_queued_cancel_are_visible(self):
        local, remote = MemorySocket(), MemorySocket()
        local.peer, remote.peer = remote, local
        state = {"executor_id": "test", "registered": False, "registered_at": None}
        runner = asyncio.create_task(executor._run_executor_connection(remote, "ws://test", state, {}, False))
        try:
            await asyncio.wait_for(local.recv(), timeout=5)
            await local.send(json.dumps({"accepted": True, "version": __version__}))
            await local.send(json.dumps({"type": "execute", "task_id": "first-id", "command": python_command("import time; time.sleep(60)")}))
            while json.loads(await asyncio.wait_for(local.recv(), timeout=5)).get("type") != "task_started":
                pass
            await local.send(json.dumps({"type": "execute", "task_id": "second-id", "command": "exit 0"}))
            await local.send(json.dumps({"type": "cancel_task", "task_id": "second-id"}))
            event = json.loads(await asyncio.wait_for(local.recv(), timeout=5))
            self.assertEqual(event["status"], "cancelled")
            self.assertIn("task first-id QUEUED", self.output.getvalue())
            self.assertIn("task first-id RUNNING · waited ", self.output.getvalue())
            self.assertIn("task second-i QUEUED", self.output.getvalue())
            self.assertIn("task second-i CANCELLED · before launch", self.output.getvalue())
            self.assertNotIn("task second-i RUNNING", self.output.getvalue())
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)


class OutputModeConfigTests(unittest.TestCase):
    def test_default_config_and_cli_override(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            config = Path(directory) / "config.yaml"
            for configured, expected in (("", "raw"), ("executor:\n  output_mode: prefixed\n", "prefixed")):
                config.write_text("broker:\n  key: test-key\n" + configured)
                arguments = ["--config", str(config), "executor", "--executor-id", "test", "--broker-url", "ws://test"]
                with patch("broker.cli.run_executor", AsyncMock()) as run:
                    main(arguments)
                    self.assertEqual(run.call_args.kwargs["output_mode"], expected)
                    main(arguments + ["--output-mode", "raw"])
                    self.assertEqual(run.call_args.kwargs["output_mode"], "raw")
            config.write_text("executor:\n  output_mode: invalid\n")
            with self.assertRaisesRegex(SystemExit, "output_mode must be raw or prefixed"):
                main(arguments)


if __name__ == "__main__":
    unittest.main()
