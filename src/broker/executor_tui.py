"""Optional interactive executor view; execution stays in executor.py."""

import asyncio
from collections import OrderedDict
import logging
import re
import time

from rich.text import Text
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Collapsible, Footer, Header, Static, TextArea, TabbedContent, TabPane
from websockets.exceptions import ConnectionClosed

from . import executor
from .logs import DEFAULT_MAX_LOG_BYTES, LogBuffer
from .version import __version__


TERMINAL = {"completed", "cancelled", "failed", "lost", "stopped"}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def plain(text):
    """Render subprocess control sequences as inert text in the interactive view."""
    text = ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(char for char in text if char in "\n\t" or char.isprintable())


def format_duration(seconds):
    """Format elapsed time, carrying rounded seconds across unit boundaries."""
    tenths = max(0, round(seconds * 10))
    days, tenths = divmod(tenths, 864000)
    hours, tenths = divmod(tenths, 36000)
    minutes, tenths = divmod(tenths, 600)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{tenths / 10:.1f}s")
    return " ".join(parts)


class RunCard(Collapsible):
    def __init__(self, run_id, kind, name, command):
        self.run_id = run_id
        self.kind = kind
        self.run_name = name
        preview = " ".join(plain(command).split())
        self.command_preview = preview if len(preview) <= 60 else preview[:59] + "…"
        self.status = "submitted" if kind == "task" else "starting"
        self.received_at = time.monotonic()
        self.started_at = None
        self.finished_at = None
        self.details = {}
        self.buffer = LogBuffer(DEFAULT_MAX_LOG_BYTES)
        self.dirty = True
        self.follow = True
        self.logs = TextArea(read_only=True, show_line_numbers=False, show_cursor=True, classes="output")
        super().__init__(
            Static(Text(plain(command)), classes="command"),
            Static("", classes="details"),
            Horizontal(Button("Cancel task" if kind == "task" else "Stop service", classes="stop", variant="warning"),
                       Button("Pause follow", classes="follow"),
                       Button("Show less", classes="collapse"), classes="controls"),
            self.logs,
            collapsed=True, id=f"run-{run_id}",
        )
        self.refresh_title()

    def on_mount(self):
        self.refresh_content()

    def on_collapsible_toggled(self, event):
        if event.collapsible is self:
            self.refresh_title()

    def refresh_title(self):
        duration = ""
        if self.started_at is not None:
            end = self.finished_at if self.finished_at is not None else time.monotonic()
            duration = f" · {format_duration(end - self.started_at)}"
        # Escape Rich markup in names coming from remote commands.
        label = plain(self.run_name).replace("[", "\\[")[:100]
        suffix = f"$ {escape(self.command_preview)}" if self.kind == "task" and self.collapsed else "Enter/click to expand or collapse"
        self.title = f"{self.kind} {label} · {self.status.upper()}{duration} · {suffix}"

    def refresh_content(self):
        if not self.is_mounted:
            return
        self.refresh_title()
        self.query_one(".stop", Button).disabled = self.status in TERMINAL or self.status in {"cancelling", "stopping"}
        details = " · ".join(f"{key}: {value}" for key, value in self.details.items())
        self.query_one(".details", Static).update(Text(plain(details)))
        if not self.dirty:
            return
        # Don't replace the text being selected while follow is paused.
        if not self.follow:
            return
        result = self.buffer.read()
        text = ("[Older local output truncated]\n" if result["truncated"] else "")
        text += plain("".join(item["data"] for item in result["entries"]))
        self.logs.load_text(text)
        if self.follow:
            self.logs.scroll_end(animate=False)
        self.dirty = False


class UILogHandler(logging.Handler):
    def __init__(self, app):
        super().__init__(logging.INFO)
        self.app = app

    def emit(self, record):
        if record.name.startswith("broker"):
            self.app.status_line.update(Text(plain(record.getMessage().replace(self.app.key, "[redacted]"))))


class ExecutorApp(App):
    TITLE = "Broker Executor"
    CSS = """
    Screen { background: #161b22; }
    #connection { height: auto; max-height: 5; padding: 1 2; background: #202936; }
    #views { height: 1fr; }
    TabPane { padding: 0; }
    .runs { height: 1fr; }
    .empty { padding: 2; color: $text-muted; }
    RunCard { margin: 0 1 1 1; border: round #35465a; padding: 0 1; }
    RunCard:focus-within { border: round #59b7ff; }
    .command { height: auto; max-height: 6; color: #62bcff; padding: 0 1; }
    .details { height: auto; max-height: 4; color: $text-muted; padding: 0 1; }
    .controls { height: 1; margin: 0 0 1 0; }
    .controls Button { margin-right: 1; width: auto; min-width: 0; height: 1; min-height: 1; border: none; padding: 0 1; }
    .output { height: 14; min-height: 5; border: none; background: #10151c; }
    """
    BINDINGS = [
        ("ctrl+q", "quit", "Quit & stop processes"),
        ("f2", "expand_all", "Expand all"),
        ("f3", "collapse_all", "Collapse all"),
    ]

    def __init__(self, broker_url, executor_id, key, runner=None):
        super().__init__()
        self.broker_url = broker_url
        self.executor_id = executor_id
        self.key = key
        self.runner = runner or executor.run_executor
        self.cards = OrderedDict()
        self.control = None
        self.status_line = Static("Connecting…", id="connection")
        self.connection_task = None
        self._exit_requested = False
        self._handler = UILogHandler(self)
        self.theme = "textual-dark"

    def compose(self) -> ComposeResult:
        yield Header()
        yield self.status_line
        with TabbedContent(initial="tasks", id="views"):
            with TabPane("Tasks", id="tasks"):
                with VerticalScroll(id="task-runs", classes="runs"):
                    yield Static("Waiting for tasks from Broker…", id="task-empty", classes="empty")
            with TabPane("Services", id="services"):
                with VerticalScroll(id="service-runs", classes="runs"):
                    yield Static("No services started yet…", id="service-empty", classes="empty")
        yield Footer()

    def on_mount(self):
        self.set_interval(0.2, self.refresh_cards)
        self.connection_task = asyncio.create_task(self.run_connection())

    async def run_connection(self):
        token = executor._UI.set(self)
        root = logging.getLogger()
        previous_handlers = root.handlers[:]
        root.handlers = [self._handler]
        try:
            await self.runner(self.broker_url, self.executor_id, interactive=False, key=self.key)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Never echo configured credentials even if a transport includes them.
            reason = str(error).replace(self.key, "[redacted]")
            self.status_line.update(Text(f"Connection failed: {plain(reason)} · Ctrl+Q to exit"))
        finally:
            self.disconnected()
            root.handlers = previous_handlers
            executor._UI.reset(token)

    def connected(self, executor_id, broker_version):
        self.executor_id = executor_id
        self.sub_title = f"{executor_id} · v{__version__}"
        self.status_line.update(Text(f"Connected · {executor_id} · broker v{broker_version or 'unknown'}\n{self.broker_url}"))

    def disconnected(self):
        self.control = None
        for card in self.cards.values():
            if card.status not in TERMINAL:
                card.status = "lost" if card.kind == "task" else "failed"
                card.finished_at = time.monotonic()
                card.details["reason"] = "Connection closed; local processes are being cleaned up"

    def event(self, event):
        kind = event.get("type")
        run_id = event.get("task_id") or event.get("service_id")
        if kind in {"execute", "start_service"}:
            card = RunCard(run_id, "task" if kind == "execute" else "service",
                           run_id[:8] if kind == "execute" else event.get("name", run_id[:8]), event["command"])
            self.cards[run_id] = card
            self.query_one(f"#{card.kind}-empty").display = False
            self.query_one(f"#{card.kind}-runs", VerticalScroll).mount(card)
        card = self.cards.get(run_id)
        if card is None:
            return
        if kind in {"output", "service_output"}:
            card.buffer.append(event["stream"], event["data"])
            card.dirty = True
        elif kind == "task_started":
            card.started_at = time.monotonic()
            card.details["waited"] = f"{card.started_at - card.received_at:.2f}s"
            if card.status != "cancelling":
                card.status = "running"
        elif kind in {"result", "service_status"}:
            status = event.get("status", "completed")
            if card.status not in TERMINAL and not (card.status == "stopping" and status in {"starting", "ready"}):
                card.status = status
                if status == "ready" and card.started_at is None:
                    card.started_at = time.monotonic()
                if status in TERMINAL:
                    card.finished_at = time.monotonic()
            for name in ("pid", "returncode", "error"):
                if name in event:
                    card.details[name] = event[name]
        elif kind in {"cancel_task", "stop_service"} and card.status not in TERMINAL:
            card.status = "cancelling" if kind == "cancel_task" else "stopping"
        self.prune_cards()

    def prune_cards(self):
        finished = [card for card in self.cards.values() if card.status in TERMINAL]
        for card in finished[:-100]:
            self.cards.pop(card.run_id)
            card.remove()

    def refresh_cards(self):
        for card in self.cards.values():
            card.refresh_content()

    async def on_button_pressed(self, event: Button.Pressed):
        card = event.button.ancestors_with_self
        card = next(widget for widget in card if isinstance(widget, RunCard))
        if event.button.has_class("collapse"):
            card.collapsed = True
        elif event.button.has_class("follow"):
            card.follow = not card.follow
            event.button.label = "Pause follow" if card.follow else "Resume follow"
            card.refresh_content()
        elif self.control is not None and card.status not in TERMINAL:
            try:
                await self.control({"type": "cancel_task" if card.kind == "task" else "stop_service",
                                    "task_id" if card.kind == "task" else "service_id": card.run_id})
            except (ConnectionClosed, OSError):
                self.status_line.update("Connection closed while sending control request; waiting for reconnect…")

    def action_expand_all(self):
        for card in self.cards.values():
            if card.kind == self._active_kind():
                card.collapsed = False

    def action_collapse_all(self):
        for card in self.cards.values():
            if card.kind == self._active_kind():
                card.collapsed = True

    def _active_kind(self):
        return "service" if self.query_one("#views", TabbedContent).active == "services" else "task"

    async def action_quit(self):
        if self._exit_requested:
            return
        self._exit_requested = True
        self.status_line.update("Stopping executor and cleaning up processes…")
        if self.connection_task:
            self.connection_task.cancel()
            await asyncio.gather(self.connection_task, return_exceptions=True)
        self.exit()

    async def on_unmount(self):
        if self.connection_task and not self.connection_task.done():
            self.connection_task.cancel()
            await asyncio.gather(self.connection_task, return_exceptions=True)
