"""Central broker dashboard, grouped by executor and backed by broker state."""

import asyncio
from contextlib import contextmanager
import hashlib
import logging
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, Footer, Header, Static, TabbedContent, TabPane

from .executor_tui import ExecutorApp, RunCard, TERMINAL, UILogHandler, plain
from .version import __version__


class ExecutorPane(TabPane):
    def compose(self):
        identifier = self.id
        yield Static("", id=f"{identifier}-info", classes="executor-info")
        with TabbedContent(id=f"{identifier}-views", classes="executor-views"):
            with TabPane("Tasks", id=f"{identifier}-tasks"):
                yield VerticalScroll(id=f"{identifier}-task", classes="runs")
            with TabPane("Services", id=f"{identifier}-services"):
                yield VerticalScroll(id=f"{identifier}-service", classes="runs")


class BrokerApp(App):
    TITLE = "Broker"
    CSS = ExecutorApp.CSS + """
    #executors { height: 1fr; }
    .executor-info { height: auto; max-height: 5; padding: 0 2; color: $text-muted; }
    .executor-views { height: 1fr; }
    """
    BINDINGS = [
        ("ctrl+q", "quit", "Quit broker"),
        ("f2", "expand_all", "Expand current view"),
        ("f3", "collapse_all", "Collapse current view"),
    ]

    def __init__(self, server, host, port, key, runner=None):
        super().__init__()
        self.server = server
        self.broker = server.execution_broker
        self.host, self.port, self.key = host, port, key
        self.runner = runner
        self.cards = {}
        self.executor_views = {}
        self.log_totals = {}
        self._sync_lock = asyncio.Lock()
        self.status_line = Static("Starting broker…", id="connection")
        self.http_server = None
        self.server_task = None
        self._exit_requested = False
        self.theme = "textual-dark"
        self.sub_title = f"v{__version__}"

    def compose(self) -> ComposeResult:
        yield Header()
        yield self.status_line
        with TabbedContent(id="executors"):
            with TabPane("Overview", id="overview"):
                yield Static("Waiting for executors…", classes="empty")
        yield Footer()

    def on_mount(self):
        self.set_interval(0.2, self.sync_state)
        self.server_task = asyncio.create_task(self.run_server())

    async def run_server(self):
        root = logging.getLogger()
        previous_handlers = root.handlers[:]
        root.handlers = [UILogHandler(self)]
        try:
            if self.runner is not None:
                await self.runner()
            else:
                import uvicorn

                class TerminalServer(uvicorn.Server):
                    @contextmanager
                    def capture_signals(self):
                        # Textual owns terminal signals and initiates shutdown.
                        yield

                self.http_server = TerminalServer(uvicorn.Config(
                    self.server.streamable_http_app(host=self.host),
                    host=self.host, port=self.port, log_level="warning", log_config=None,
                    timeout_graceful_shutdown=5,
                ))
                await self.http_server.serve()
            if not self._exit_requested:
                self.status_line.update("Broker stopped · Ctrl+Q to exit")
        except asyncio.CancelledError:
            raise
        except (Exception, SystemExit) as error:
            reason = plain(str(error).replace(self.key, "[redacted]"))
            self.status_line.update(Text(f"Broker failed: {reason} · Ctrl+Q to exit"))
        finally:
            root.handlers = previous_handlers

    async def sync_state(self):
        async with self._sync_lock:
            if self._exit_requested:
                return
            await self._sync_state()

    async def _sync_state(self):
        known = set(self.broker.executors)
        known.update(record["executor_id"] for record in self.broker.tasks.values())
        known.update(record["executor_id"] for record in self.broker.services.values())
        tabs = self.query_one("#executors", TabbedContent)
        for executor_id in sorted(known):
            if executor_id not in self.executor_views:
                identifier = "executor-" + hashlib.sha256(executor_id.encode()).hexdigest()[:16]
                self.executor_views[executor_id] = identifier
                pane = ExecutorPane(plain(executor_id).replace("[", "\\["), id=identifier)
                await tabs.add_pane(pane)
                if tabs.active == "overview" and len(self.executor_views) == 1:
                    tabs.active = identifier
            identifier = self.executor_views[executor_id]
            record = self.broker.executors.get(executor_id)
            info = f"{executor_id} · {'ONLINE' if record else 'OFFLINE'}"
            if record:
                info += f" · v{record.get('version', 'unknown')}\n{record.get('cwd', 'unknown')} · {record.get('python', 'unknown')}"
            self.query_one(f"#{identifier}-info", Static).update(Text(plain(info)))

        retained_ids = set()
        for kind, records in (("task", list(self.broker.tasks.values())), ("service", list(self.broker.services.values()))):
            for record in records:
                run_id = record[f"{kind}_id"]
                retained_ids.add(run_id)
                executor_id = record["executor_id"]
                identifier = self.executor_views[executor_id]
                card = self.cards.get(run_id)
                if card is None:
                    card = RunCard(run_id, kind, run_id[:8] if kind == "task" else record["name"], record["command"])
                    card.executor_id = executor_id
                    card.buffer = record["output" if kind == "task" else "logs"]
                    self.cards[run_id] = card
                    await self.query_one(f"#{identifier}-{kind}", VerticalScroll).mount(card)
                card.status = record["status"]
                card.details = {key: record[key] for key in ("pid", "returncode", "error") if key in record}
                if kind == "task":
                    # Cards use monotonic time; translate broker wall-clock timestamps.
                    offset = time.monotonic() - time.time()
                    card.started_at = record["started_at"] + offset if record["started_at"] else None
                    card.finished_at = record["finished_at"] + offset if record["finished_at"] else None
                    if record["started_at"]:
                        card.details["waited"] = f"{record['started_at'] - record['submitted_at']:.2f}s"
                elif card.status == "ready" and card.started_at is None:
                    card.started_at = time.monotonic()
                elif card.status in TERMINAL and card.finished_at is None:
                    card.finished_at = time.monotonic()
                if self.log_totals.get(run_id) != card.buffer.total:
                    card.dirty = True
                    self.log_totals[run_id] = card.buffer.total
                card.refresh_content()

        for run_id in list(self.cards):
            if run_id not in retained_ids:
                await self.cards.pop(run_id).remove()
                self.log_totals.pop(run_id, None)
        for executor_id in list(self.executor_views):
            if executor_id not in known:
                await tabs.remove_pane(self.executor_views.pop(executor_id))
        online = len(self.broker.executors)
        self.query_one("#overview", TabPane).query_one(Static).update(Text(
            f"MCP: {self.host}:{self.port}/mcp\n{online} online executor(s) · "
            f"{len(self.broker.tasks)} task(s) · {len(self.broker.services)} service(s)"
        ))
        if (self.http_server is not None and self.http_server.started and not self._exit_requested
                and self.server_task is not None and not self.server_task.done()):
            self.status_line.update(Text(f"Broker v{__version__} · MCP {self.host}:{self.port}/mcp · {online} online executor(s)"))

    async def on_button_pressed(self, event: Button.Pressed):
        card = next(widget for widget in event.button.ancestors_with_self if isinstance(widget, RunCard))
        if event.button.has_class("collapse"):
            card.collapsed = True
        elif event.button.has_class("follow"):
            card.follow = not card.follow
            event.button.label = "Pause follow" if card.follow else "Resume follow"
            card.refresh_content()
        elif card.status not in TERMINAL:
            try:
                if card.kind == "task":
                    await self.broker.cancel_task(card.run_id)
                else:
                    await self.broker.stop_service(card.run_id)
                await self.sync_state()
            except Exception:
                self.status_line.update("Control request failed; check executor connection and current state")

    def current_cards(self):
        active = self.query_one("#executors", TabbedContent).active
        if active == "overview":
            return []
        views = self.query_one(f"#{active}-views", TabbedContent)
        kind = "service" if views.active.endswith("-services") else "task"
        executor_id = next(name for name, identifier in self.executor_views.items() if identifier == active)
        return [card for card in self.cards.values() if card.executor_id == executor_id and card.kind == kind]

    def action_expand_all(self):
        for card in self.current_cards():
            card.collapsed = False

    def action_collapse_all(self):
        for card in self.current_cards():
            card.collapsed = True

    async def stop_server(self):
        if self.http_server is not None:
            self.http_server.should_exit = True
            await self.broker.shutdown()
        if self.server_task and not self.server_task.done():
            if self.http_server is None:
                self.server_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self.server_task), timeout=10)
            except asyncio.TimeoutError:
                self.server_task.cancel()
                await asyncio.gather(self.server_task, return_exceptions=True)
            except asyncio.CancelledError:
                pass

    async def action_quit(self):
        if self._exit_requested:
            return
        self._exit_requested = True
        self.status_line.update("Closing broker and executor connections…")
        await self.stop_server()
        self.exit()

    async def on_unmount(self):
        await self.stop_server()
