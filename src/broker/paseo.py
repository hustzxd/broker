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
    required = ("paseo_server_id", "paseo_agent_id")
    if any(not isinstance(target.get(key), str) or not target[key].strip() for key in required):
        raise ValueError("paseo requires non-empty paseo_server_id and paseo_agent_id")
    allowed = set(required) | {"paseo_host"}
    if set(target) - allowed:
        raise ValueError(f"unknown paseo fields: {', '.join(sorted(set(target) - allowed))}")
    result = {key: target[key].strip() for key in required}
    if "paseo_host" in target:
        if not isinstance(target["paseo_host"], str) or not target["paseo_host"].strip():
            raise ValueError("paseo_host must be a non-empty string")
        result["paseo_host"] = target["paseo_host"].strip()
    return result


async def send_completion(target, prompt, *, executable="paseo", timeout=DEFAULT_SEND_TIMEOUT):
    command = shutil.which(executable)
    if command is None:
        raise FileNotFoundError(f"Paseo CLI not found: {executable}")
    args = [command]
    if target.get("paseo_host"):
        args.extend(("--host", target["paseo_host"]))
    args.extend(("send", target["paseo_agent_id"], "--prompt", prompt,
                 "--behavior", "steer", "--no-wait", "--json"))
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise TimeoutError(f"paseo send timed out after {timeout} seconds") from exc
    if process.returncode:
        detail = stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(detail or f"paseo send exited with {process.returncode}")
    result = stdout.decode("utf-8", "replace").strip()
    return json.loads(result) if result else {"status": "submitted"}


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
