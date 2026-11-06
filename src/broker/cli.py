"""Command-line entry points for the execution broker and executor."""

import argparse
import asyncio
import logging
import os
import json
from urllib.parse import urlsplit, urlunsplit

from .broker import create_broker
from .config import DEFAULT_CONFIG_PATH, ensure_broker_key, ensure_config, int_value, load_config
from .auth import validate_key
from .executor import BrokerConnectionError, run_executor
from .logs import DEFAULT_MAX_LOG_BYTES
from .doctor import diagnose
from .version import __version__


def _build_parser():
    parser = argparse.ArgumentParser(prog="broker")
    parser.add_argument("--version", action="version", version=f"%(prog)s v{__version__}")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--mcp-host")
    parser.add_argument("--mcp-port", type=int)
    parser.add_argument("--executor-host")
    parser.add_argument("--executor-port", type=int)
    parser.add_argument("--max-log-bytes", type=int, help="retained UTF-8 log bytes per task or service")
    subparsers = parser.add_subparsers(dest="command")
    parser.set_defaults(command="broker")

    executor = subparsers.add_parser("executor", help="start an executor")
    executor.add_argument("--broker-url")
    executor.add_argument(
        "--port",
        "--broker-port",
        dest="broker_port",
        type=int,
        help="override the port in the broker WebSocket URL",
    )
    executor.add_argument("--executor-id")
    executor.add_argument("--output-mode", choices=("raw", "prefixed"), help="local terminal output format")
    doctor = subparsers.add_parser("doctor", help="check MCP connectivity, key and tool discovery")
    doctor.add_argument("--url", help="MCP URL; defaults to the configured broker listener")
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "doctor":
        # Diagnostics must not generate a new key or rewrite configuration.
        config = load_config(args.config)
        broker_config = config.get("broker", {})
        key = os.environ.get("BROKER_KEY", broker_config.get("key"))
        try:
            validate_key(key)
        except ValueError:
            raise SystemExit("Doctor requires BROKER_KEY or an existing broker.key; configuration was not modified") from None
        host = broker_config.get("mcp_host", "127.0.0.1")
        host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        url = args.url or f"http://{host}:{int_value(broker_config, 'mcp_port', 8273)}/mcp"
        parsed_url = urlsplit(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise SystemExit("Doctor URL must be HTTP(S) without credentials, query or fragment; put the key in BROKER_KEY")
        # httpx INFO logs can include URLs; diagnostics print only sanitized results.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        result = asyncio.run(diagnose(url, key))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
        return
    ensure_config(args.config)
    config = load_config(args.config)
    if args.command == "broker":
        broker_config = config.get("broker", {})
        try:
            key = validate_key(os.environ["BROKER_KEY"]) if "BROKER_KEY" in os.environ else ensure_broker_key(args.config)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        host = args.mcp_host or broker_config.get("mcp_host", "127.0.0.1")
        mcp_port = args.mcp_port or int_value(broker_config, "mcp_port", 8273)
        executor_host = args.executor_host or broker_config.get("executor_host", "127.0.0.1")
        executor_port = args.executor_port or int_value(broker_config, "executor_port", 8272)
        max_log_bytes = args.max_log_bytes
        if max_log_bytes is None:
            max_log_bytes = broker_config.get("max_log_bytes", DEFAULT_MAX_LOG_BYTES)
        if isinstance(max_log_bytes, bool) or not isinstance(max_log_bytes, int) or max_log_bytes <= 0:
            raise SystemExit("max_log_bytes must be a positive integer")
        create_broker(
            executor_host=executor_host,
            executor_port=executor_port,
            max_log_bytes=max_log_bytes,
            key=key,
            max_completed_services=broker_config.get("max_completed_services", 100),
            max_completed_tasks=broker_config.get("max_completed_tasks", 100),
            max_pending_tasks=broker_config.get("max_pending_tasks", 32),
        ).run(
            "streamable-http",
            host=host,
            port=mcp_port,
        )
        return
    executor_config = config.get("executor", {})
    output_mode = args.output_mode or executor_config.get("output_mode", "raw")
    if output_mode not in {"raw", "prefixed"}:
        raise SystemExit("executor.output_mode must be raw or prefixed")
    key = os.environ.get("BROKER_KEY", executor_config.get("key", config.get("broker", {}).get("key")))
    try:
        validate_key(key)
    except ValueError:
        raise SystemExit("Executor requires BROKER_KEY or executor.key matching the central broker key") from None
    broker_url = args.broker_url or executor_config.get("broker_url")
    broker_port = args.broker_port
    if broker_port is None and executor_config.get("broker_port") is not None:
        broker_port = int_value(executor_config, "broker_port", 8272)
    if broker_port is not None:
        if not 1 <= broker_port <= 65535:
            raise SystemExit("executor broker port must be between 1 and 65535")
        if broker_url:
            parsed_url = urlsplit(broker_url)
            if not parsed_url.hostname:
                raise SystemExit("executor broker URL must include a host")
            host = parsed_url.hostname
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            userinfo = ""
            if parsed_url.username is not None:
                userinfo = parsed_url.username
                if parsed_url.password is not None:
                    userinfo += f":{parsed_url.password}"
                userinfo += "@"
            broker_url = urlunsplit(
                parsed_url._replace(netloc=f"{userinfo}{host}:{broker_port}")
            )
    executor_id = args.executor_id
    if not broker_url:
        raise SystemExit("executor requires --broker-url or a value in config.yaml")
    if not executor_id:
        executor_id = input("Executor name: ").strip()
    if not executor_id:
        raise SystemExit("executor name cannot be empty")
    try:
        asyncio.run(run_executor(broker_url, executor_id, interactive=True, key=key, output_mode=output_mode))
    except BrokerConnectionError as exc:
        raise SystemExit(
            f"Cannot connect to central broker: {broker_url}\n"
            "Start it on the broker server with `broker`, then retry.\n"
            "For remote executors, ensure the broker listens on a reachable interface "
            "(e.g. `broker --executor-host 0.0.0.0`).\n"
            f"Check --broker-url or {args.config} and the Executor WebSocket "
            "port (default: 8272).\n"
            f"Connection error: {exc}"
        ) from None
