"""Persistent agent connection to the hub.

One session sends several commands and collects scene events between them.
The one-shot ``client`` module stays available for manual checks.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import websockets

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from protocol.messages import MAX_FRAME_BYTES, command, dumps, hello, loads  # noqa: E402


class HubSession:
    def __init__(self, uri: str, timeout: float = 30.0) -> None:
        self.uri = uri
        self.timeout = timeout
        self.session_id = ""
        self.events: list[dict[str, Any]] = []
        self._ws: Any = None

    async def __aenter__(self) -> "HubSession":
        self._ws = await websockets.connect(
            self.uri,
            open_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=MAX_FRAME_BYTES,
        )
        try:
            await self._greet()
        except Exception:
            await self._ws.close()
            self._ws = None
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def request(self, op: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("session is closed")
        cmd = command(op, payload or {})
        await self._ws.send(dumps(cmd))
        deadline = time.monotonic() + self.timeout
        while True:
            msg = await self._recv(deadline)
            kind = msg.get("type")
            if kind == "event":
                self.events.append(msg)
                continue
            if kind == "result" and msg.get("id") == cmd["id"]:
                return msg
            if kind == "error":
                raise RuntimeError(str(msg.get("error") or "hub error"))

    async def _greet(self) -> None:
        assert self._ws is not None
        await self._ws.send(
            dumps(hello("agent", "blenderai-loop", capabilities=["command", "event"]))
        )
        deadline = time.monotonic() + self.timeout
        acked = False
        while True:
            msg = await self._recv(deadline)
            kind = msg.get("type")
            if kind == "hello_ack":
                acked = True
                self.session_id = str(msg.get("session_id") or "")
            elif kind == "peer_status" and acked and "blender" in (msg.get("roles") or []):
                return
            elif kind == "error":
                raise RuntimeError(str(msg.get("error") or "hub error"))

    async def _recv(self, deadline: float) -> dict[str, Any]:
        assert self._ws is not None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("等待中枢超时")
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
        except TimeoutError as exc:
            raise TimeoutError("等待中枢超时") from exc
        return loads(raw)
