"""YAML configuration for the execution CLI."""

import os
import copy
import secrets
import tempfile
import hashlib
from urllib.parse import urlsplit

import yaml

from .logs import DEFAULT_MAX_LOG_BYTES
from .auth import validate_key


DEFAULT_CONFIG_PATH = "~/.broker/config.yaml"
DEFAULT_CONFIG = {
    "broker": {
        "output_mode": "raw",
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
        "output_mode": "raw",
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


class ConfigConflictError(ValueError):
    """The configuration changed after the editor loaded it."""


def _read_web_config(path):
    path = os.path.expanduser(path)
    if os.path.islink(path):
        raise ValueError("Cannot edit a symbolic-link configuration file")
    with open(path, "rb") as config_file:
        raw = config_file.read()
    config = yaml.safe_load(raw) or {}
    if not isinstance(config, dict) or any(
        not isinstance(config.get(section, {}), dict) for section in DEFAULT_CONFIG
    ):
        raise ValueError("Configuration sections must be objects")
    return path, config, hashlib.sha256(raw).hexdigest()


def web_config_snapshot(path):
    """Return editable saved settings without disclosing stored credentials."""
    path, config, revision = _read_web_config(path)
    values = {}
    for section, defaults in DEFAULT_CONFIG.items():
        stored = config.get(section, {})
        values[section] = {name: stored.get(name, default) for name, default in defaults.items()}
        if section == "executor":
            values[section]["broker_port"] = stored.get("broker_port")
    return {
        "path": os.path.abspath(path), "revision": revision, "values": values,
        "key_configured": {section: bool(config.get(section, {}).get("key")) for section in DEFAULT_CONFIG},
        "environment_key_override": "BROKER_KEY" in os.environ,
    }


def _validate_web_setting(section, name, value):
    label = f"{section}.{name}"
    if name == "key":
        return validate_key(value)
    if name.endswith("port") or name.startswith("max_"):
        if section == "executor" and name == "broker_port" and value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{label} must be a positive integer")
        if name.endswith("port") and value > 65535:
            raise ValueError(f"{label} must be between 1 and 65535")
        return value
    if not isinstance(value, str) or not value or len(value) > 2048 or any(char.isspace() for char in value):
        raise ValueError(f"{label} must be a non-empty string without whitespace")
    if name == "output_mode":
        choices = {"raw", "tui"} if section == "broker" else {"raw", "prefixed", "tui"}
        if value not in choices:
            raise ValueError(f"Invalid {label}")
    elif name == "broker_url":
        try:
            url = urlsplit(value)
            port = url.port
            if url.scheme not in {"ws", "wss"} or not url.hostname or url.username or url.password or url.fragment or (port is not None and port < 1):
                raise ValueError()
        except ValueError:
            raise ValueError("executor.broker_url must be a WS/WSS URL without credentials or fragment") from None
    elif name.endswith("host") and any(char in value for char in "/@?#"):
        raise ValueError(f"{label} must be a host name or IP address, without a scheme or path")
    return value


def save_web_config(path, values, revision):
    """Validate and atomically merge settings, preserving keys and unrelated fields."""
    path, config, current_revision = _read_web_config(path)
    if not isinstance(revision, str) or revision != current_revision:
        raise ConfigConflictError("Configuration changed; reload it before saving")
    if not isinstance(values, dict) or set(values) - set(DEFAULT_CONFIG):
        raise ValueError("Expected broker and executor configuration objects")
    for section, settings in values.items():
        allowed = set(DEFAULT_CONFIG[section]) | {"key"}
        if section == "executor":
            allowed.add("broker_port")
        if not isinstance(settings, dict) or set(settings) - allowed:
            raise ValueError(f"Unknown settings in {section}")
        target = config.setdefault(section, {})
        for name, value in settings.items():
            value = _validate_web_setting(section, name, value)
            if value is None:
                target.pop(name, None)
            else:
                target[name] = value
    descriptor, temporary_path = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
            yaml.safe_dump(config, config_file, sort_keys=False)
            config_file.flush()
            os.fsync(config_file.fileno())
        # A notification binding or external edit must not be overwritten.
        if _read_web_config(path)[2] != current_revision:
            raise ConfigConflictError("Configuration changed; reload it before saving")
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return web_config_snapshot(path)
