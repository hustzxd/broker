import asyncio
import importlib.util
import unittest
from unittest.mock import patch
import os
from pathlib import Path
import tempfile

from broker.broker import ExecutionBroker
from broker.version import __version__
from broker.cli import main
from test_cancel_task import MemorySocket, python_command

HAS_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAS_TEXTUAL:
    from broker.executor_tui import ExecutorApp, RunCard, TabbedContent, format_duration


async def idle_runner(*args, **kwargs):
    await asyncio.Event().wait()


@unittest.skipUnless(HAS_TEXTUAL, "Install execution-broker[tui] to test the interactive UI")
class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_collapse_keyboard_expand_scroll_and_text_selection(self):
        app = ExecutorApp("ws://test", "demo", "secret", runner=idle_runner)
        async with app.run_test(size=(110, 38)) as pilot:
            app.connected("demo", __version__)
            app.event({"type": "execute", "task_id": "first", "command": "python train.py"})
            app.event({"type": "task_started", "task_id": "first"})
            app.event({"type": "output", "task_id": "first", "stream": "stdout", "data": "".join(f"line {n}\n" for n in range(100))})
            await pilot.pause(0.3)
            card = app.cards["first"]
            self.assertTrue(card.collapsed)
            self.assertIn("$ python train.py", card.title)
            self.assertIn("line 99", card.logs.text)
            await pilot.click("CollapsibleTitle")
            await pilot.pause(0.3)
            self.assertFalse(card.collapsed)
            self.assertNotIn("$ python train.py", card.title)
            self.assertEqual(card.query_one(".command").render().plain, "python train.py")
            await pilot.click(".collapse")
            await pilot.pause(0.3)
            self.assertTrue(card.collapsed)
            await pilot.click("CollapsibleTitle")
            await pilot.pause(0.3)
            self.assertFalse(card.collapsed)
            await pilot.press("f3")
            self.assertTrue(card.collapsed)
            await pilot.press("f2")
            await pilot.click(".follow")
            self.assertFalse(card.follow)
            card.logs.focus()
            card.logs.move_cursor((0, 0))
            await pilot.press("shift+end")
            self.assertEqual(card.logs.selected_text, "line 0")
            await pilot.press("ctrl+c")
            self.assertEqual(app.clipboard, "line 0")
            app.event({"type": "output", "task_id": "first", "stream": "stderr", "data": "new output"})
            await pilot.pause(0.3)
            self.assertEqual(card.logs.selected_text, "line 0")
            self.assertNotIn("new output", card.logs.text)
            card.logs.scroll_end(animate=False)
            await pilot.pause(0.3)
            self.assertGreater(card.logs.scroll_y, 0)
            await pilot.click(".follow")
            await pilot.pause(0.3)
            self.assertIn("new output", card.logs.text)
            await app.action_quit()

    async def test_limited_logs_history_and_inert_remote_markup(self):
        app = ExecutorApp("ws://test", "demo", "secret", runner=idle_runner)
        async with app.run_test(size=(100, 35)) as pilot:
            app.event({"type": "start_service", "service_id": "svc", "name": "[red]unsafe\x07", "command": "echo [bold]literal"})
            app.event({"type": "service_output", "service_id": "svc", "stream": "stderr", "data": "\x1b[31mhello\x1b[0m\rworld\n"})
            await pilot.pause(0.3)
            card = app.cards["svc"]
            self.assertEqual(card.logs.text, "hello\nworld\n")
            self.assertNotIn("\x07", card.title)
            card.buffer.max_bytes = 8
            app.event({"type": "service_output", "service_id": "svc", "stream": "stdout", "data": "x" * 1000})
            app.event({"type": "service_status", "service_id": "svc", "status": "failed", "error": "startup failed", "returncode": 2})
            await pilot.pause(0.3)
            self.assertIn("truncated", card.logs.text)
            self.assertLessEqual(card.buffer.size, 8)
            self.assertTrue(card.query_one(".stop").disabled)
            for number in range(102):
                run_id = f"task{number}"
                app.event({"type": "execute", "task_id": run_id, "command": "exit 0"})
                app.event({"type": "result", "task_id": run_id, "status": "completed", "returncode": 0})
            await pilot.pause(0.3)
            self.assertEqual(len(app.cards), 100)
            self.assertEqual(len(app.query(RunCard)), 100)
            await app.action_quit()

    async def test_real_execution_local_cancel_service_stop_and_exit_cleanup(self):
        broker = ExecutionBroker()
        local, remote = MemorySocket(), MemorySocket()
        local.peer, remote.peer = remote, local
        broker_loop = asyncio.create_task(broker.handle_executor(local))

        async def connect(*args, **kwargs):
            return remote

        async def wait_until(predicate):
            async def poll():
                while not predicate():
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(poll(), timeout=5)

        app = ExecutorApp("ws://test", "demo", "test-key")
        with patch("broker.executor.connect", connect):
            try:
                async with app.run_test(size=(115, 45)) as pilot:
                    await wait_until(lambda: "demo" in broker.executors and app.control is not None)
                    first = await broker.execute("demo", python_command("import time; print('ready', flush=True); time.sleep(60)"), timeout=0)
                    run_id = first["task_id"]
                    await wait_until(lambda: broker.get_task(run_id)["status"] == "running")
                    queued = await broker.execute("demo", "printf should-not-start", timeout=0)
                    await wait_until(lambda: queued["task_id"] in app.cards)
                    with patch("builtins.print") as terminal_print:
                        await app.control({"type": "cancel_task", "task_id": queued["task_id"]})
                        terminal_print.assert_not_called()
                    await wait_until(lambda: broker.get_task(queued["task_id"])["status"] == "cancelled")
                    app.cards[queued["task_id"]].collapsed = True
                    app.cards[run_id].collapsed = False
                    await pilot.pause()
                    await pilot.click(f"#run-{run_id} .stop")
                    await wait_until(lambda: broker.get_task(run_id)["status"] == "cancelled")
                    self.assertEqual(app.cards[run_id].status, "cancelled")
                    self.assertTrue(app.cards[run_id].buffer.read()["entries"] or not broker.get_task(run_id)["output"])
                    app.action_collapse_all()
                    service = await broker.start_service("demo", "demo-service", python_command("import time; print('service ready', flush=True); time.sleep(60)"))
                    service_id = service["service_id"]
                    await wait_until(lambda: broker.get_service(service_id)["status"] == "ready")
                    app.query_one("#views", TabbedContent).active = "services"
                    app.cards[service_id].collapsed = False
                    await pilot.pause()
                    app.cards[service_id].scroll_visible(animate=False)
                    await pilot.pause()
                    await pilot.click(f"#run-{service_id} .stop")
                    await wait_until(lambda: broker.get_service(service_id)["status"] == "stopped")
                    active = await broker.execute("demo", python_command("import time; time.sleep(60)"), timeout=0)
                    await wait_until(lambda: broker.get_task(active["task_id"])["status"] == "running")
                    await app.action_quit()
                await asyncio.wait_for(broker_loop, timeout=5)
                self.assertIn(broker.get_task(active["task_id"])["status"], {"lost", "cancelled"})
            finally:
                await local.close()
                await asyncio.gather(broker_loop, return_exceptions=True)

    async def test_reconnect_status_and_failure_redacts_key(self):
        async def failed_runner(*args, **kwargs):
            raise executor.BrokerConnectionError("invalid test-secret")

        from broker import executor
        app = ExecutorApp("ws://test", "demo", "test-secret", runner=failed_runner)
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause(0.3)
            self.assertNotIn("test-secret", str(app.status_line.render()))
            self.assertIn("redacted", str(app.status_line.render()))
            app.event({"type": "execute", "task_id": "old", "command": "sleep 60"})
            app.disconnected()
            self.assertEqual(app.cards["old"].status, "lost")
            app.connected("demo", __version__)
            app.event({"type": "execute", "task_id": "new", "command": "exit 0"})
            self.assertEqual(app.cards["new"].status, "submitted")
            self.assertEqual(app.cards["old"].status, "lost")
            await app.action_quit()

    async def test_task_service_tabs_keep_independent_views_and_controls(self):
        app = ExecutorApp("ws://test", "demo", "secret", runner=idle_runner)
        async with app.run_test(size=(100, 35)) as pilot:
            app.event({"type": "execute", "task_id": "task", "command": "sleep 10"})
            app.event({"type": "start_service", "service_id": "service", "name": "server", "command": "server"})
            await pilot.pause(0.3)
            task, service = app.cards["task"], app.cards["service"]
            self.assertEqual(task.parent.id, "task-runs")
            self.assertEqual(service.parent.id, "service-runs")
            self.assertEqual(app.query_one("#views", TabbedContent).active, "tasks")
            self.assertTrue(task.collapsed)
            self.assertTrue(service.collapsed)
            service.collapsed = False
            await pilot.press("f3")
            self.assertTrue(task.collapsed)
            self.assertFalse(service.collapsed)
            await pilot.click("#--content-tab-services")
            await pilot.pause(0.3)
            self.assertEqual(app.query_one("#views", TabbedContent).active, "services")
            await pilot.press("f3")
            self.assertTrue(service.collapsed)
            await pilot.press("f2")
            self.assertFalse(service.collapsed)
            self.assertTrue(task.collapsed)
            app.event({"type": "output", "task_id": "task", "stream": "stdout", "data": "background output"})
            await pilot.click("#--content-tab-tasks")
            await pilot.pause(0.3)
            self.assertTrue(task.collapsed)
            self.assertIn("background output", task.logs.text)
            await app.action_quit()


@unittest.skipUnless(HAS_TEXTUAL, "Install execution-broker[tui] to test the interactive UI")
class TuiCliTests(unittest.TestCase):
    def test_collapsed_task_command_preview(self):
        for command, preview in (
            ("ls -la", "ls -la"),
            ("echo first\n  &&\t echo second", "echo first && echo second"),
            ("x" * 60, "x" * 60),
            ("x" * 61, "x" * 59 + "…"),
            ("测" * 61, "测" * 59 + "…"),
        ):
            with self.subTest(command=command):
                card = RunCard("preview", "task", "preview", command)
                self.assertIn("$ " + preview, card.title)
                self.assertLessEqual(len(card.command_preview), 60)
                card.collapsed = False
                card.refresh_title()
                self.assertNotIn("$ " + preview, card.title)
        card = RunCard("markup", "task", "markup", "echo [red]literal[/red]")
        self.assertIn(r"\[red]", card.title)
        service = RunCard("service", "service", "vllm", "serve model")
        self.assertNotIn("$ serve model", service.title)

    def test_duration_units_and_rounding_boundaries(self):
        for seconds, expected in (
            (0, "0.0s"), (-1, "0.0s"), (59.94, "59.9s"),
            (59.96, "1m 0.0s"), (60, "1m 0.0s"),
            (1111.4, "18m 31.4s"), (3599.96, "1h 0m 0.0s"),
            (3661.2, "1h 1m 1.2s"), (86399.96, "1d 0h 0m 0.0s"),
            (90061.3, "1d 1h 1m 1.3s"),
        ):
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration(seconds), expected)

    def test_card_duration_refreshes_and_freezes_at_completion(self):
        card = RunCard("duration-test", "service", "test", "serve")
        card.started_at = 100
        with patch("broker.executor_tui.time.monotonic", return_value=1211.4):
            card.refresh_title()
            self.assertIn("18m 31.4s", card.title)
        with patch("broker.executor_tui.time.monotonic", return_value=3761.2):
            card.refresh_title()
            self.assertIn("1h 1m 1.2s", card.title)
            card.finished_at = 160
            card.refresh_title()
            self.assertIn("1m 0.0s", card.title)

    def test_tui_requires_terminal_and_launches_app_with_existing_config(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / "config.yaml"
            path.write_text("broker:\n  key: test-key\nexecutor:\n  broker_url: ws://test\n")
            arguments = ["--config", str(path), "executor", "--executor-id", "demo"]
            with patch("sys.stdin.isatty", return_value=False):
                with self.assertRaisesRegex(SystemExit, "interactive terminal"):
                    main(arguments)
            with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True):
                with patch("broker.executor_tui.ExecutorApp") as app:
                    main(arguments)
                    app.assert_called_once_with("ws://test", "demo", "test-key")
                    app.return_value.run.assert_called_once()

    def test_new_config_defaults_to_tui(self):
        from broker.config import ensure_config, load_config

        with tempfile.TemporaryDirectory() as directory:
            path = ensure_config(str(Path(directory) / "config.yaml"))
            self.assertEqual(load_config(path)["executor"]["output_mode"], "tui")


if __name__ == "__main__":
    unittest.main()
