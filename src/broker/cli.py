"""Command-line entry points for the execution broker and executor."""

import argparse
import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit

from .broker import create_broker
from .config import DEFAULT_CONFIG_PATH, ensure_config, int_value, load_config
from .executor import BrokerConnectionError, run_executor


def _build_parser():
    parser = argparse.ArgumentParser(prog="broker")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--mcp-host")
    parser.add_argument("--mcp-port", type=int)
    parser.add_argument("--executor-host")
    parser.add_argument("--executor-port", type=int)
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
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ensure_config(args.config)
    config = load_config(args.config)
    if args.command == "broker":
        broker_config = config.get("broker", {})
        host = args.mcp_host or broker_config.get("mcp_host", "127.0.0.1")
        mcp_port = args.mcp_port or int_value(broker_config, "mcp_port", 8273)
        executor_host = args.executor_host or broker_config.get("executor_host", "127.0.0.1")
        executor_port = args.executor_port or int_value(broker_config, "executor_port", 8272)
        create_broker(
            executor_host=executor_host,
            executor_port=executor_port,
        ).run(
            "streamable-http",
            host=host,
            port=mcp_port,
        )
        return
    executor_config = config.get("executor", {})
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
        asyncio.run(run_executor(broker_url, executor_id, interactive=True))
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
