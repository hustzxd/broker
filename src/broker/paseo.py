"""Optional Paseo completion notifications for long-running services."""

import asyncio
import json
import logging
import shutil

logger = logging.getLogger(__name__)
DEFAULT_SEND_TIMEOUT = 300


def validate_target(target):
    if target is None:
        return None
    if not isinstance(target, dict):
        raise ValueError("paseo must be an object")
    required = ("paseo_agent_id",)
    if any(not isinstance(target.get(key), str) or not target[key].strip() for key in required):
        raise ValueError("paseo requires a non-empty paseo_agent_id")
    allowed = set(required) | {"paseo_host", "paseo_server_id"}
    if set(target) - allowed:
        raise ValueError(f"unknown paseo fields: {', '.join(sorted(set(target) - allowed))}")
    result = {key: target[key].strip() for key in required}
    for key in ("paseo_host", "paseo_server_id"):
        if key in target:
            if not isinstance(target[key], str) or not target[key].strip():
                raise ValueError(f"{key} must be a non-empty string")
            result[key] = target[key].strip()
    return result


async def _run_cli(target, arguments, *, executable="paseo", timeout=DEFAULT_SEND_TIMEOUT):
    command = shutil.which(executable)
    if command is None:
        raise FileNotFoundError(f"Paseo CLI not found: {executable}")
    args = [command]
    if target.get("paseo_host"):
        args.extend(("--host", target["paseo_host"]))
    args.extend(arguments)
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise TimeoutError(f"paseo {arguments[0]} timed out after {timeout} seconds") from exc
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        detail = stderr.decode("utf-8", "replace").strip() or stdout.decode("utf-8", "replace").strip()
        raise RuntimeError(detail or f"paseo {arguments[0]} exited with {process.returncode}")
    result = stdout.decode("utf-8", "replace").strip()
    if not result:
        raise RuntimeError(f"paseo {arguments[0]} returned no receipt")
    receipt = json.loads(result)
    if not isinstance(receipt, dict) or receipt.get("error") or receipt.get("success") is False:
        raise RuntimeError(f"paseo {arguments[0]} returned an error receipt")
    return receipt


async def wait_until_idle(target, *, executable="paseo"):
    """Wait in bounded CLI calls so busy agents remain pending until idle."""
    while True:
        receipt = await _run_cli(target, ("wait", target["paseo_agent_id"], "--timeout", "30", "--json"),
                                 executable=executable, timeout=45)
        status = receipt.get("status")
        if status == "idle":
            return
        if status not in {"timeout", "permission"}:
            raise RuntimeError(f"Paseo agent is not available for notification: {status}")
        await asyncio.sleep(2)


async def send_completion(target, prompt, *, executable="paseo", timeout=DEFAULT_SEND_TIMEOUT):
    return await _run_cli(target, ("send", target["paseo_agent_id"], "--prompt", prompt,
                                  "--behavior", "steer", "--no-wait", "--json"),
                          executable=executable, timeout=timeout)


def completion_prompt(service, event_id):
    service_id = service["service_id"]
    error = service.get("error")
    error_text = f", error={error}" if error else ""
    return (
        f"Broker service 已结束：service_id={service_id}, name={service['name']}, "
        f"status={service['status']}, returncode={service.get('returncode')!r}"
        f"{error_text}, finished_at={service.get('finished_at')}。"
        f"如需完整输出，请调用 get_service_logs({service_id})。event_id={event_id}"
    )
