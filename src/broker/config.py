"""YAML configuration for the execution CLI."""

import os
import copy
import secrets
import tempfile

import yaml

from .logs import DEFAULT_MAX_LOG_BYTES
from .auth import validate_key


DEFAULT_CONFIG_PATH = "~/.broker/config.yaml"
DEFAULT_CONFIG = {
    "broker": {
        "output_mode": "tui",
        "mcp_host": "127.0.0.1",
        "mcp_port": 8273,
        "executor_host": "127.0.0.1",
        "executor_port": 8272,
        "max_log_bytes": DEFAULT_MAX_LOG_BYTES,
        "max_completed_services": 100,
        "max_completed_tasks": 100,
        "max_pending_tasks": 32,
    },
    "executor": {
        "broker_url": "ws://127.0.0.1:8272",
        "output_mode": "tui",
    },
}


def ensure_config(path=DEFAULT_CONFIG_PATH):
    path = os.path.expanduser(path)
    is_default = os.path.abspath(path) == os.path.abspath(os.path.expanduser(DEFAULT_CONFIG_PATH))
    parent = os.path.dirname(path)
    if is_default:
        if os.path.islink(parent) or os.path.islink(path):
            raise ValueError("Default broker configuration directory and file must not be symbolic links")
        os.makedirs(parent, mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
    if os.path.exists(path):
        if is_default:
            os.chmod(path, 0o600)
        return path

    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as config_file:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["broker"]["key"] = secrets.token_urlsafe(32)
        yaml.safe_dump(config, config_file, sort_keys=False)
    return path


def ensure_broker_key(path=DEFAULT_CONFIG_PATH):
    """Add a key to an older config atomically, retaining its other settings."""
    path = ensure_config(path)
    with open(path) as config_file:
        config = yaml.safe_load(config_file) or {}
    broker_config = config.setdefault("broker", {})
    if os.path.islink(path):
        raise ValueError("Cannot use a symbolic-link configuration file for the broker key")
    if "key" in broker_config:
        key = validate_key(broker_config["key"])
        os.chmod(path, 0o600)
        return key
    key = secrets.token_urlsafe(32)
    broker_config["key"] = key
    descriptor, temporary_path = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(descriptor, "w") as config_file:
            yaml.safe_dump(config, config_file, sort_keys=False)
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return key


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


def save_executor_paseo(path, bindings):
    """Atomically update notification defaults without replacing other settings."""
    path = os.path.expanduser(path)
    if os.path.islink(path):
        raise ValueError("Cannot update a symbolic-link configuration file")
    with open(path, encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}
    config.setdefault("broker", {})["executor_paseo"] = bindings
    descriptor, temporary_path = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
            yaml.safe_dump(config, config_file, sort_keys=False)
            config_file.flush()
            os.fsync(config_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
