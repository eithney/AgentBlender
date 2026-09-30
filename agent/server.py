"""Local WebSocket hub between the agent process and Blender.

One connection slot per role (``agent``, ``blender``). A new login replaces
the previous socket of the same role. Commands travel agent -> blender.
Results and scene events travel blender -> agent.

Bind address defaults to 127.0.0.1. The addon executes scene ops, so this
port should stay on the loopback interface.

    python -m agent.server
    python -m agent.server --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

import websockets
from websockets.asyncio.server import ServerConnection

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from protocol.messages import (  # noqa: E402
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    ROLES,
    dumps,
    error_envelope,
    hello_ack,
    loads,
    peer_status,
    result,
)

log = logging.getLogger("blenderai.hub")


class Hub:
    def __init__(self) -> None:
        self.session_id = uuid.uuid4().hex
        self._peers: dict[str, ServerConnection] = {}
        self._caps: dict[str, list[str]] = {}
        self._peer_ids: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def attach(self, role: str, websocket: ServerConnection, hello_msg: dict) -> str:
        peer_id = f"{role}-{uuid.uuid4().hex[:8]}"
        caps = hello_msg.get("capabilities") or []
        if not isinstance(caps, list):
            caps = []
        caps = [str(item) for item in caps][:32]
        async with self._lock:
            previous = self._peers.get(role)
            self._peers[role] = websocket
            self._caps[role] = caps
            self._peer_ids[role] = peer_id
        if previous is not None and previous is not websocket:
            log.info("replacing previous %s connection", role)
            try:
                await previous.close(code=4000, reason="replaced")
            except Exception:
                log.debug("previous %s socket already closed", role)
        await websocket.send(dumps(hello_ack(self.session_id, peer_id)))
        log.info(
            "hello role=%s name=%s peer=%s caps=%s",
            role,
            hello_msg.get("name"),
            peer_id,
            caps,
        )
        await self.broadcast()
        self._log_handshake()
        return peer_id

    async def detach(self, role: str, websocket: ServerConnection) -> None:
        removed = False
        async with self._lock:
            if self._peers.get(role) is websocket:
                self._peers.pop(role, None)
                self._caps.pop(role, None)
                self._peer_ids.pop(role, None)
                removed = True
        if removed:
            log.info("peer left role=%s", role)
            await self.broadcast()

    async def broadcast(self) -> None:
        async with self._lock:
            payload = dumps(peer_status(self.session_id, dict(self._caps)))
            targets = list(self._peers.items())
        for role, websocket in targets:
            try:
                await websocket.send(payload)
            except websockets.ConnectionClosed:
                log.debug("peer_status skipped, %s already closed", role)
            except Exception as exc:
                log.warning("peer_status to %s failed: %s", role, exc)

    async def route(self, role: str, websocket: ServerConnection, msg: dict) -> None:
        msg_type = msg.get("type")
        if msg_type == "command" and role == "agent":
            await self._forward_command(websocket, msg)
            return
        if msg_type in ("result", "event") and role == "blender":
            await self._forward_to_agent(msg)
            return
        await self._safe_send(
            websocket,
            error_envelope(f"unexpected message type {msg_type!r} from {role}", msg.get("id")),
        )

    async def _forward_command(self, origin: ServerConnection, msg: dict) -> None:
        cmd_id = msg.get("id")
        op = msg.get("op")
        if not isinstance(cmd_id, str) or not isinstance(op, str):
            await self._safe_send(origin, error_envelope("command requires string id and op"))
            return
        async with self._lock:
            blender = self._peers.get("blender")
        if blender is None:
            await self._safe_send(origin, result(cmd_id, ok=False, error="blender offline"))
            log.info("command %s dropped: blender offline", op)
            return
        try:
            await blender.send(dumps(msg))
        except Exception as exc:
            await self._safe_send(origin, result(cmd_id, ok=False, error=f"blender send failed: {exc}"))
            return
        log.info("command %s -> blender id=%s", op, cmd_id[:8])

    async def _forward_to_agent(self, msg: dict) -> None:
        async with self._lock:
            agent = self._peers.get("agent")
        if agent is None:
            log.info("drop %s: agent offline", msg.get("type"))
            return
        try:
            await agent.send(dumps(msg))
        except Exception as exc:
            log.warning("forward to agent failed: %s", exc)
            return
        label = msg.get("op") or msg.get("name") or msg.get("type")
        log.info("blender -> agent %s", label)

    def _log_handshake(self) -> None:
        if {"agent", "blender"} <= set(self._peers):
            log.info("bidirectional handshake complete: agent <-> blender session=%s", self.session_id)

    @staticmethod
    async def _safe_send(websocket: ServerConnection, msg: dict) -> None:
        try:
            await websocket.send(dumps(msg))
        except Exception as exc:
            log.debug("send failed: %s", exc)


async def _handler(hub: Hub, websocket: ServerConnection) -> None:
    role: str | None = None
    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=10)
        try:
            hello_msg = loads(raw)
        except ValueError as exc:
            await websocket.send(dumps(error_envelope(str(exc))))
            await websocket.close(code=1008, reason="bad hello")
            return
        if hello_msg.get("type") != "hello":
            await websocket.send(dumps(error_envelope("first message must be hello")))
            await websocket.close(code=1008, reason="expected hello")
            return
        role = hello_msg.get("role")
        if role not in ROLES:
            await websocket.send(dumps(error_envelope("role must be agent or blender")))
            await websocket.close(code=1008, reason="unknown role")
            return
        await hub.attach(role, websocket, hello_msg)
        async for raw in websocket:
            try:
                msg = loads(raw)
            except ValueError as exc:
                await websocket.send(dumps(error_envelope(str(exc))))
                continue
            if msg.get("type") == "hello":
                await websocket.send(dumps(error_envelope("already greeted")))
                continue
            await hub.route(role, websocket, msg)
    except TimeoutError:
        log.info("peer timed out before hello")
    except websockets.ConnectionClosed:
        log.info("connection closed role=%s", role)
    except Exception:
        log.exception("connection error role=%s", role)
    finally:
        if role is not None:
            await hub.detach(role, websocket)


async def serve(host: str, port: int) -> None:
    hub = Hub()

    async def handler(websocket: ServerConnection) -> None:
        await _handler(hub, websocket)

    log.info("BlenderAI-OS hub %s listening on ws://%s:%s", PROTOCOL_VERSION, host, port)
    log.info("waiting for peers: agent, blender")
    async with websockets.serve(
        handler,
        host,
        port,
        ping_interval=20,
        ping_timeout=20,
        max_size=MAX_FRAME_BYTES,
    ):
        await asyncio.Future()


def main() -> None:
    parser = argparse.ArgumentParser(description="BlenderAI-OS WebSocket hub")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(serve(args.host, args.port))
    except KeyboardInterrupt:
        log.info("hub stopped")


if __name__ == "__main__":
    main()
