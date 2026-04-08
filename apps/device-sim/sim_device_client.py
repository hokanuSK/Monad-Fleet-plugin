#!/usr/bin/env python3
import json
import logging
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone

import grpc

import fleet_gateway_pb2
import fleet_gateway_pb2_grpc


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sim-device")


def now_ms() -> int:
    return int(time.time() * 1000)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def bounded_tail(text: str, max_bytes: int) -> str:
    if not text:
        return ""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return raw.decode("utf-8", errors="replace")
    return raw[-max_bytes:].decode("utf-8", errors="replace")


def parse_range_time(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def in_range(range_obj: dict) -> bool:
    if not isinstance(range_obj, dict):
        return True

    from_ts = parse_range_time(str(range_obj.get("from", "")))
    to_ts = parse_range_time(str(range_obj.get("to", "")))
    now_ts = time.time()

    if from_ts is not None and now_ts < from_ts:
        return False
    if to_ts is not None and now_ts > to_ts:
        return False
    return True


def iter_commands(group: dict) -> list[dict]:
    commands = group.get("commands", [])
    if isinstance(commands, list):
        return [cmd for cmd in commands if isinstance(cmd, dict)]

    # Also support object style commands as used in older drafts.
    if isinstance(commands, dict):
        rows: list[dict] = []
        for command_id, payload in commands.items():
            if not isinstance(payload, dict):
                continue
            row = {"id": command_id, **payload}
            rows.append(row)
        rows.sort(key=lambda row: int(row.get("order", 999999)))
        return rows

    return []


def publish_event(stub, payload: dict, retries: int = 5) -> bool:
    for attempt in range(1, retries + 1):
        try:
            response = stub.PublishEvent(fleet_gateway_pb2.Event(**payload), timeout=10)
            if response.ok:
                logger.info("Published event %s (%s)", payload["event_id"], payload.get("type", ""))
                return True
            logger.warning("PublishEvent returned not-ok: %s", response.message)
        except grpc.RpcError as exc:
            logger.warning("PublishEvent attempt %d/%d failed: %s", attempt, retries, exc)
        time.sleep(min(2 * attempt, 8))
    return False


def execute_command(command: dict, execute_shell: bool, max_stdout_bytes: int, max_stderr_bytes: int):
    command_id = str(command.get("id") or command.get("name") or f"cmd-{uuid.uuid4().hex[:8]}")
    cmd = str(command.get("cmd") or command.get("command") or "echo no-op")
    timeout_s = int(command.get("timeout_s", 60))

    started = time.time()
    if not execute_shell:
        time.sleep(min(1, timeout_s))
        return {
            "command_id": command_id,
            "exit_code": 0,
            "stdout_tail": f"simulated command: {cmd}",
            "stderr_tail": "",
            "duration_ms": int((time.time() - started) * 1000),
        }

    try:
        proc = subprocess.run(  # noqa: S602
            cmd,
            shell=True,
            timeout=timeout_s,
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            "command_id": command_id,
            "exit_code": int(proc.returncode),
            "stdout_tail": bounded_tail(proc.stdout or "", max_stdout_bytes),
            "stderr_tail": bounded_tail(proc.stderr or "", max_stderr_bytes),
            "duration_ms": int((time.time() - started) * 1000),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command_id": command_id,
            "exit_code": 124,
            "stdout_tail": bounded_tail(exc.stdout or "", max_stdout_bytes),
            "stderr_tail": bounded_tail((exc.stderr or "") + "\ncommand timed out", max_stderr_bytes),
            "duration_ms": int((time.time() - started) * 1000),
        }


def run_policy(stub, device_id: str, policy: dict, policy_revision: str, max_stdout_bytes: int, max_stderr_bytes: int, execute_shell: bool) -> None:
    experiment_id = str(policy.get("experiment_id", ""))
    groups = policy.get("command_groups", [])
    if not isinstance(groups, list):
        groups = []

    total_commands = 0
    failed_commands = 0

    for group in groups:
        if not isinstance(group, dict):
            continue
        if not in_range(group.get("range", {})):
            continue

        for command in iter_commands(group):
            total_commands += 1
            command_id = str(command.get("id") or command.get("name") or f"command-{total_commands}")

            started_payload = {
                "event_id": str(uuid.uuid4()),
                "experiment_id": experiment_id,
                "policy_revision": policy_revision,
                "device_id": device_id,
                "unix_time_ms": now_ms(),
                "type": "COMMAND_STARTED",
                "command_id": command_id,
                "exit_code": 0,
                "stdout_tail": "",
                "stderr_tail": "",
                "metrics": {},
                "artifacts": {},
            }
            publish_event(stub, started_payload)

            retries = int(command.get("retries", 0))
            max_attempts = retries + 1
            result = None
            for attempt in range(1, max_attempts + 1):
                result = execute_command(command, execute_shell, max_stdout_bytes, max_stderr_bytes)
                if result["exit_code"] == 0:
                    break
                logger.warning(
                    "Command %s failed on attempt %d/%d with exit_code=%s",
                    command_id,
                    attempt,
                    max_attempts,
                    result["exit_code"],
                )

            if result is None:
                continue

            if result["exit_code"] != 0:
                failed_commands += 1

            finished_payload = {
                "event_id": str(uuid.uuid4()),
                "experiment_id": experiment_id,
                "policy_revision": policy_revision,
                "device_id": device_id,
                "unix_time_ms": now_ms(),
                "type": "COMMAND_FINISHED",
                "command_id": result["command_id"],
                "exit_code": int(result["exit_code"]),
                "stdout_tail": result["stdout_tail"],
                "stderr_tail": result["stderr_tail"],
                "metrics": {
                    "duration_ms": str(result["duration_ms"]),
                },
                "artifacts": {},
            }
            publish_event(stub, finished_payload)

    run_state = "COMPLETED" if failed_commands == 0 else "PARTIAL"
    final_payload = {
        "event_id": str(uuid.uuid4()),
        "experiment_id": experiment_id,
        "policy_revision": policy_revision,
        "device_id": device_id,
        "unix_time_ms": now_ms(),
        "type": "COMMAND_FINISHED",
        "command_id": "__run__",
        "exit_code": 0 if failed_commands == 0 else 1,
        "stdout_tail": f"run completed at {now_iso()}",
        "stderr_tail": "",
        "metrics": {
            "run_state": run_state,
            "commands_total": str(total_commands),
            "commands_failed": str(failed_commands),
        },
        "artifacts": {},
    }
    publish_event(stub, final_payload)


def main():
    device_id = os.environ.get("DEVICE_ID") or os.environ.get("DEVICE_MAC") or "02:42:ac:14:00:05"
    fleet_host = os.environ.get("FLEET_MANAGER_HOST") or os.environ.get("GATEWAY_HOST") or "monad-fleet-service"
    fleet_port = int(os.environ.get("FLEET_MANAGER_PORT") or os.environ.get("GATEWAY_PORT") or "50060")

    agent_version = os.environ.get("AGENT_VERSION", "0.1.0")
    device_type = os.environ.get("DEVICE_TYPE", "rpi-dualband")
    execute_shell = os.environ.get("EXECUTE_POLICY", "false").strip().lower() in {"1", "true", "yes"}

    max_stdout_bytes = int(os.environ.get("MAX_STDOUT_BYTES", "4096"))
    max_stderr_bytes = int(os.environ.get("MAX_STDERR_BYTES", "4096"))
    max_sync_cycles = int(os.environ.get("MAX_SYNC_CYCLES", "1"))
    default_poll_seconds = int(os.environ.get("DEFAULT_POLL_SECONDS", "30"))

    capabilities = {
        "csi": os.environ.get("CAP_CSI", "1"),
        "ble": os.environ.get("CAP_BLE", "1"),
    }

    target = f"{fleet_host}:{fleet_port}"
    logger.info("Simulated device starting: device_id=%s target=%s", device_id, target)

    channel = grpc.insecure_channel(target)
    stub = fleet_gateway_pb2_grpc.FleetManagerStub(channel)

    hello = None
    for attempt in range(1, 31):
        try:
            hello = stub.Hello(
                fleet_gateway_pb2.AgentInfo(
                    device_id=device_id,
                    agent_version=agent_version,
                    device_type=device_type,
                    capabilities=capabilities,
                    unix_time_ms=now_ms(),
                ),
                timeout=10,
            )
            logger.info(
                "Hello ok: poll_interval_s=%s min_agent_version=%s",
                hello.poll_interval_s,
                hello.required_min_agent_version,
            )
            break
        except grpc.RpcError as exc:
            logger.warning("Hello attempt %d/30 failed: %s", attempt, exc)
            time.sleep(2)

    if hello is None:
        logger.error("Unable to connect to Fleet Manager")
        raise SystemExit(1)

    poll_interval = hello.poll_interval_s if hello.poll_interval_s > 0 else default_poll_seconds
    last_policy_revision = ""

    cycle = 0
    cycle_total_label = str(max_sync_cycles) if max_sync_cycles > 0 else "infinite"
    while max_sync_cycles <= 0 or cycle < max_sync_cycles:
        cycle += 1
        logger.info("Policy sync cycle %d/%s", cycle, cycle_total_label)
        response = stub.GetPolicy(
            fleet_gateway_pb2.PolicyRequest(
                device_id=device_id,
                last_policy_revision=last_policy_revision,
            ),
            timeout=15,
        )

        if response.not_modified or not response.policy_json:
            logger.info("No policy update (revision=%s)", response.policy_revision)
            time.sleep(poll_interval)
            continue

        policy = json.loads(response.policy_json.decode("utf-8"))
        last_policy_revision = response.policy_revision
        logger.info(
            "Received policy revision=%s experiment_id=%s",
            response.policy_revision,
            policy.get("experiment_id", ""),
        )

        run_policy(
            stub=stub,
            device_id=device_id,
            policy=policy,
            policy_revision=response.policy_revision,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            execute_shell=execute_shell,
        )
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
