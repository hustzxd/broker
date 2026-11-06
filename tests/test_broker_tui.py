import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from broker.broker import create_broker
from broker.cli import main

HAS_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAS_TEXTUAL:
    from broker.broker_tui import BrokerApp
    from broker.executor_tui import TabbedContent


async def idle():
    await asyncio.Event().wait()


@unittest.skipUnless(HAS_TEXTUAL, "Textual required")
class BrokerTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_views_are_independent_and_share_bounded_output(self):
        server = create_broker(key="secret")
        broker = server.execution_broker
        for name in ("gpu/a", "gpu-b"):
            broker.executors[name] = {"executor_id": name, "cwd": "/" + name, "version": "test",
                                      "websocket": SimpleNamespace(send=AsyncMock())}
        app = BrokerApp(server, "127.0.0.1", 8273, "secret", runner=idle)
        async with app.run_test(size=(110, 40)) as pilot:
            first = await broker.execute("gpu/a", "sleep 60", timeout=0)
            second = await broker.execute("gpu-b", "sleep 60", timeout=0)
            service = await broker.start_service("gpu/a", "vllm", "serve")
            await pilot.pause(0.3)
            await app.sync_state()
            first_card, second_card = app.cards[first["task_id"]], app.cards[second["task_id"]]
            self.assertNotEqual(first_card.parent.id, second_card.parent.id)
            self.assertTrue(first_card.collapsed)
            self.assertTrue(second_card.collapsed)
            self.assertIs(first_card.buffer, broker.tasks[first["task_id"]]["output"])
            first_card.buffer.append("stdout", "incremental output")
            await app.sync_state()
            self.assertIn("incremental output", first_card.logs.text)
            identifier = app.executor_views["gpu/a"]
            app.query_one("#executors", TabbedContent).active = identifier
            await pilot.press("f2")
            self.assertFalse(first_card.collapsed)
            self.assertTrue(second_card.collapsed)
            self.assertTrue(app.cards[service["service_id"]].collapsed)
            await pilot.click(f"#run-{first['task_id']} .stop")
            self.assertEqual(broker.get_task(first["task_id"])["status"], "cancelling")
            await pilot.click(f"#--content-tab-{identifier}-services")
            await pilot.pause(0.3)
            await pilot.press("f2")
            await pilot.pause(0.3)
            await pilot.click(f"#run-{service['service_id']} .stop")
            self.assertEqual(broker.get_service(service["service_id"])["status"], "stopping")
            broker.executors.pop("gpu/a")
            broker._mark_task_lost(broker.tasks[first["task_id"]])
            await app.sync_state()
            self.assertEqual(first_card.status, "lost")
            self.assertIn("OFFLINE", str(app.query_one(f"#{identifier}-info").render()))
            del broker.tasks[first["task_id"]]
            await app.sync_state()
            self.assertNotIn(first["task_id"], app.cards)
            await app.action_quit()

    async def test_empty_overview_and_startup_failure_redact_key(self):
        async def failed():
            raise OSError("cannot bind secret")

        app = BrokerApp(create_broker(key="secret"), "127.0.0.1", 8273, "secret", runner=failed)
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause(0.3)
            self.assertIn("redacted", str(app.status_line.render()))
            self.assertNotIn("secret", str(app.status_line.render()))
            self.assertEqual(app.executor_views, {})
            await app.action_quit()

    async def test_server_start_and_quit_close_connections_without_signal_capture(self):
        class FakeServer:
            def __init__(self, config):
                self.started = False
                self.should_exit = False
                self.config = config

            async def serve(self):
                with self.capture_signals():
                    self.started = True
                    while not self.should_exit:
                        await asyncio.sleep(0.01)

        server = create_broker(key="secret")
        connection = SimpleNamespace(close=AsyncMock())
        server.execution_broker.executors["demo"] = {"websocket": connection}
        with patch("uvicorn.Server", FakeServer):
            app = BrokerApp(server, "127.0.0.1", 8273, "secret")
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.pause(0.3)
                self.assertTrue(app.http_server.started)
                await app.action_quit()
                self.assertTrue(app.http_server.should_exit)
                self.assertTrue(app.server_task.done())
                connection.close.assert_awaited_once()
                self.assertEqual(server.execution_broker.executors, {})


@unittest.skipUnless(HAS_TEXTUAL, "Textual required")
class BrokerTuiCliTests(unittest.TestCase):
    def test_default_tui_raw_override_and_nonterminal_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / "config.yaml"
            path.write_text("broker:\n  key: secret\n")
            args = ["--config", str(path)]
            with patch("broker.cli.create_broker") as server:
                with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True):
                    with patch("broker.broker_tui.BrokerApp") as app:
                        main(args)
                        app.assert_called_once_with(server.return_value, "127.0.0.1", 8273, "secret")
                        app.return_value.run.assert_called_once()
                        server.return_value.run.assert_not_called()
                main(args + ["--output-mode", "raw"])
                server.return_value.run.assert_called_once_with("streamable-http", host="127.0.0.1", port=8273)
                with patch("sys.stdin.isatty", return_value=False):
                    with self.assertRaisesRegex(SystemExit, "interactive terminal"):
                        main(args)


if __name__ == "__main__":
    unittest.main()
