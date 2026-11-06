"""Messages exchanged between the execution broker and executors."""

import uuid
import math


def request_id():
    return uuid.uuid4().hex


def message(message_type, **payload):
    return {"type": message_type, **payload}


def validate_service(name, command, ready_check, startup_timeout):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Service name must be a non-empty string")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Service command must be a non-empty string")
    if (isinstance(startup_timeout, bool) or not isinstance(startup_timeout, (int, float))
            or not math.isfinite(startup_timeout) or startup_timeout <= 0):
        raise ValueError("startup_timeout must be a positive finite number")
    if ready_check is None:
        return
    if not isinstance(ready_check, dict) or ready_check.get("type") != "tcp":
        raise ValueError("ready_check must be a tcp check object or null")
    port = ready_check.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("ready_check.port must be an integer between 1 and 65535")
    host = ready_check.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("ready_check.host must be a non-empty string")
