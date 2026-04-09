#!/usr/bin/env python3
"""Small MCP smoke client for stdio servers."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


def _send(proc: subprocess.Popen[bytes], payload: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("child stdin is not available")
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
    proc.stdin.write(header)
    proc.stdin.write(raw)
    proc.stdin.flush()


def _read(proc: subprocess.Popen[bytes]) -> dict[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("child stdout is not available")

    headers: dict[str, str] = {}
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("unexpected EOF from MCP server")
        if line in {b"\r\n", b"\n"}:
            break
        text = line.decode("utf-8", errors="replace").strip()
        if ":" in text:
            key, value = text.split(":", 1)
            headers[key.lower().strip()] = value.strip()

    length_raw = headers.get("content-length", "")
    if not length_raw.isdigit():
        raise RuntimeError(f"missing content-length header in response: {headers}")
    length = int(length_raw)
    body = proc.stdout.read(length)
    if not body or len(body) != length:
        raise RuntimeError("invalid response body length from MCP server")
    return json.loads(body.decode("utf-8"))


def _request(proc: subprocess.Popen[bytes], req_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        },
    )
    reply = _read(proc)
    if reply.get("id") != req_id:
        raise RuntimeError(f"response id mismatch: expected={req_id} got={reply.get('id')}")
    if "error" in reply:
        raise RuntimeError(f"{method} failed: {reply['error']}")
    result = reply.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{method} returned invalid result payload")
    return result


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test an MCP stdio server.")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to execute after '--'. Default: python3 tools/elab-mcp/server.py",
    )
    args = parser.parse_args()

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        command = ["python3", "tools/elab-mcp/server.py"]

    print(f"[mcp-smoke] command: {' '.join(command)}")
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        init = _request(
            proc,
            1,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "mcp-smoke", "version": "1.0.0"},
                "capabilities": {},
            },
        )
        _assert("serverInfo" in init, "initialize result missing serverInfo")
        print(f"[mcp-smoke] initialize ok: {init['serverInfo']}")

        resources = _request(proc, 2, "resources/list", {})
        resource_uris = {row.get("uri") for row in resources.get("resources", []) if isinstance(row, dict)}
        _assert("elab://config" in resource_uris, "resources/list missing elab://config")
        print("[mcp-smoke] resources/list ok")

        templates = _request(proc, 3, "resources/templates/list", {})
        template_uris = {
            row.get("uriTemplate")
            for row in templates.get("resourceTemplates", [])
            if isinstance(row, dict)
        }
        _assert(
            "elab://fleet/diagnostics?device_id={id}&run_id={run_id}" in template_uris,
            "resources/templates/list missing diagnostics template",
        )
        print("[mcp-smoke] resources/templates/list ok")

        tools = _request(proc, 4, "tools/list", {})
        tool_names = {row.get("name") for row in tools.get("tools", []) if isinstance(row, dict)}
        _assert("elab_read" in tool_names, "tools/list missing elab_read")
        _assert("elab_get_latest_policy" in tool_names, "tools/list missing elab_get_latest_policy")
        print("[mcp-smoke] tools/list ok")

        read_config = _request(
            proc,
            5,
            "tools/call",
            {"name": "elab_read", "arguments": {"uri": "elab://config"}},
        )
        structured = read_config.get("structuredContent")
        _assert(isinstance(structured, dict), "tools/call(elab_read config) missing structuredContent")
        _assert("base_url" in structured, "config payload missing base_url")
        _assert("api_key_present" in structured, "config payload missing api_key_present")
        print("[mcp-smoke] tools/call elab_read(elab://config) ok")

        read_help = _request(
            proc,
            6,
            "tools/call",
            {"name": "elab_read", "arguments": {"uri": "elab://diagnostics/help"}},
        )
        help_payload = read_help.get("structuredContent")
        _assert(isinstance(help_payload, dict), "help payload missing structuredContent")
        _assert(help_payload.get("server") == "fleetmanager-elab-mcp", "unexpected help.server")
        print("[mcp-smoke] tools/call elab_read(elab://diagnostics/help) ok")

        _request(proc, 99, "shutdown", {})
        print("[mcp-smoke] shutdown ok")
    finally:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

        stderr_text = ""
        if proc.stderr is not None:
            stderr_text = proc.stderr.read().decode("utf-8", errors="replace").strip()
        if stderr_text:
            print(f"[mcp-smoke] stderr:\n{stderr_text}", file=sys.stderr)

    if proc.returncode not in (0, None):
        raise RuntimeError(f"MCP process exited with code {proc.returncode}")

    print("[mcp-smoke] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
