"""Zero-dependency MCP (Model Context Protocol) stdio server for FM27 Studio + Tactics Lab.

Exposes the complete FM27 Studio toolset (live/snapshot player & club queries,
lineup optimization, squad tactic planner, S24 training schedules, FM-Arena online
tactics, AND native FM27 Tactics Lab match-engine A/B & multi-tactic benchmarks)
to any MCP-compatible AI client (Claude Desktop, Claude Code, Cursor, Antigravity).

Uses an isolated Tactics Lab data directory (``tactics-lab-mcp``) so external MCP
clients never collide with FM27 Studio's single-instance exclusive lease.

Usage:
    python -S fm27_mcp_server.py
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, BinaryIO

# Ensure repo root is on sys.path even under ``python -S``
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fmrte import aiprompt, fmtools, tacticslab  # noqa: E402
from fmrte.db import Snapshot  # noqa: E402

SERVER_NAME = "fm27-studio"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"


def _mcp_tools() -> list[dict[str, Any]]:
    """Convert OpenAI-style fmtools catalogue into MCP Tool definitions."""
    out: list[dict[str, Any]] = []
    for spec in fmtools.tool_definitions():
        fn = spec.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "description": str(fn.get("description") or ""),
            "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


class FM27McpServer:
    """Pure-stdlib JSON-RPC 2.0 MCP server over stdio."""

    def __init__(self, db: Snapshot | None = None, lab: tacticslab.TacticsLabClient | None = None):
        self._owns_db = db is None
        self.db = db if db is not None else Snapshot()
        self.lab = lab if lab is not None else tacticslab.TacticsLabClient(data_subdir="tactics-lab-mcp")
        fmtools.set_lab_client(self.lab)
        self._default_club = aiprompt.resolve_active_club(self.db)

    def close(self) -> None:
        try:
            self.lab.stop()
        except Exception:
            pass
        if self._owns_db:
            try:
                self.db.close()
            except Exception:
                pass

    def handle_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Process one JSON-RPC 2.0 request/notification; return response dict or None."""
        if not isinstance(msg, dict):
            return self._error(None, -32600, "Invalid Request")
        req_id = msg.get("id")
        method = str(msg.get("method") or "")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

        # Notifications have no id -> no response required
        if req_id is None:
            return None

        if method == "initialize":
            client_ver = str(params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION)
            return self._result(req_id, {
                "protocolVersion": client_ver,
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
                "instructions": aiprompt.INSTRUCTIONS,
            })

        if method == "ping":
            return self._result(req_id, {})

        if method == "tools/list":
            return self._result(req_id, {"tools": _mcp_tools()})

        if method == "tools/call":
            tool_name = str(params.get("name") or "").strip()
            arguments = params.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            if not tool_name:
                return self._error(req_id, -32602, "Tool name is required")
            try:
                res = fmtools.execute_tool(
                    self.db, tool_name, arguments, default_club=self._default_club)
                is_err = isinstance(res, dict) and bool(res.get("error"))
                text = json.dumps(res, ensure_ascii=False, default=str)
                return self._result(req_id, {
                    "content": [{"type": "text", "text": text}],
                    "isError": is_err,
                })
            except Exception as exc:  # noqa: BLE001
                return self._result(req_id, {
                    "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                    "isError": True,
                })

        if method in ("resources/list", "prompts/list"):
            key = "resources" if method.startswith("resources") else "prompts"
            return self._result(req_id, {key: []})

        return self._error(req_id, -32601, f"Method not found: {method}")

    @staticmethod
    def _result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _read_message(stream: BinaryIO) -> tuple[dict[str, Any] | None, str]:
    """Read one MCP JSON-RPC message supporting both NDJSON and Content-Length framing."""
    while True:
        line = stream.readline()
        if not line:
            return None, "ndjson"
        stripped = line.strip()
        if not stripped:
            continue
        # Legacy Content-Length framing
        if stripped.lower().startswith(b"content-length:"):
            try:
                length = int(stripped.split(b":", 1)[1].strip())
            except ValueError:
                continue
            # Consume remaining headers until blank line
            while True:
                hdr = stream.readline()
                if not hdr or hdr in (b"\r\n", b"\n"):
                    break
            body = stream.read(length)
            if not body:
                return None, "lsp"
            return json.loads(body.decode("utf-8")), "lsp"
        # Standard MCP stdio: newline-delimited JSON
        return json.loads(stripped.decode("utf-8")), "ndjson"


def _write_message(stream: BinaryIO, payload: dict[str, Any], framing: str = "ndjson") -> None:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if framing == "lsp":
        header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
        stream.write(header + raw)
    else:
        stream.write(raw + b"\n")
    stream.flush()


def serve_stdio(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    inp = stdin if stdin is not None else sys.stdin.buffer
    out = stdout if stdout is not None else sys.stdout.buffer
    server = FM27McpServer()
    try:
        while True:
            try:
                msg, framing = _read_message(inp)
            except (ValueError, UnicodeDecodeError) as exc:
                _write_message(out, server._error(None, -32700, f"Parse error: {exc}"))
                continue
            if msg is None:
                break
            resp = server.handle_message(msg)
            if resp is not None:
                _write_message(out, resp, framing=framing)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(serve_stdio())
