"""JSON envelopes carried over the WebSocket hub.

The hub, the agent, and the Blender addon all speak this format. Keeping it
in one module stops the two processes from drifting apart.

Envelope
    {"v", "type", "ts", ...}

Types
    hello         peer -> hub, first message on a connection
    hello_ack     hub -> peer
    peer_status   hub -> all peers, whenever membership changes
    command       agent -> hub -> blender
    result        blender -> hub -> agent
    event         blender -> hub -> agent
    error         hub -> peer
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

PROTOCOL_VERSION = "0.1.0"

ROLES = ("agent", "blender")
COMMAND_OPS = ("ping", "get_scene", "create_mesh", "render_preview")
MAX_FRAME_BYTES = 4 * 1024 * 1024


def new_id() -> str:
    return uuid.uuid4().hex


def envelope(msg_type: str, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": msg_type,
        "ts": time.time(),
    }
    body.update(fields)
    return body


def hello(role: str, name: str, capabilities: list[str] | None = None) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    caps = [str(item) for item in (capabilities or [])][:32]
    return envelope("hello", id=new_id(), role=role, name=name, capabilities=caps)


def hello_ack(session_id: str, peer_id: str) -> dict[str, Any]:
    return envelope("hello_ack", session_id=session_id, peer_id=peer_id)


def peer_status(session_id: str, peers: dict[str, list[str]]) -> dict[str, Any]:
    return envelope(
        "peer_status",
        session_id=session_id,
        roles=sorted(peers),
        peers=peers,
    )


def command(op: str, payload: dict[str, Any] | None = None, cmd_id: str | None = None) -> dict[str, Any]:
    return envelope("command", id=cmd_id or new_id(), op=op, payload=payload or {})


def result(
    cmd_id: str,
    ok: bool,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    body = envelope("result", id=cmd_id, ok=ok, payload=payload or {})
    if error:
        body["error"] = error
    return body


def event(name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return envelope("event", id=new_id(), name=name, payload=payload or {})


def error_envelope(message: str, ref: str | None = None) -> dict[str, Any]:
    return envelope("error", id=ref or new_id(), ok=False, error=message)


def dumps(msg: dict[str, Any]) -> str:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"))


def loads(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    msg = json.loads(raw)
    if not isinstance(msg, dict) or "type" not in msg:
        raise ValueError("invalid envelope")
    if msg.get("v") != PROTOCOL_VERSION:
        raise ValueError(f"protocol mismatch: {msg.get('v')!r}")
    return msg
