"""YAML configuration for the execution CLI."""

import os

import yaml


DEFAULT_CONFIG_PATH = "~/.broker/config.yaml"
DEFAULT_CONFIG = {
    "broker": {
        "mcp_host": "127.0.0.1",
        "mcp_port": 8273,
        "executor_host": "127.0.0.1",
        "executor_port": 8272,
    },
    "executor": {
        "broker_url": "ws://127.0.0.1:8272",
    },
}


def ensure_config(path=DEFAULT_CONFIG_PATH):
    path = os.path.expanduser(path)
    if os.path.exists(path):
        return path

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "x") as config_file:
        yaml.safe_dump(DEFAULT_CONFIG, config_file, sort_keys=False)
    return path


def load_config(path=DEFAULT_CONFIG_PATH):
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return {}

    with open(path, "r") as config_file:
        config = yaml.safe_load(config_file) or {}
    return {
        section: config.get(section, {})
        for section in ("broker", "executor")
        if isinstance(config.get(section, {}), dict)
    }


def int_value(section, name, default):
    value = section.get(name)
    return default if value is None else int(value)
