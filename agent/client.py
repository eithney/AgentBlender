"""Agent-side peer, plus a Blender stand-in for handshake checks.

Agent mode greets the hub, waits until a blender peer is online, sends one
command, and prints the result. ``--role blender-sim`` answers ``ping`` and
``get_scene`` without Blender so the socket path can be checked first.

    python -m agent.client --role blender-sim
    python -m agent.client --op ping
    python -m agent.client --op get_scene
    python -m agent.client --op create_mesh --name Cup
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import websockets

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from protocol.messages import MAX_FRAME_BYTES, command, dumps, hello, loads, result  # noqa: E402

BLENDER_OPS = ("ping", "get_scene", "create_mesh", "render_preview")
# 1x1 PNG so the preview path can be exercised without bpy.
TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _payload_for(args: argparse.Namespace) -> dict[str, Any]:
    if args.op == "create_mesh":
        return {
            "primitive": args.primitive,
            "name": args.name,
            "size": args.size,
            "location": [args.x, args.y, args.z],
        }
    if args.op == "render_preview":
        return {"engine": "WORKBENCH", "width": 256, "height": 256}
    return {}


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            return


async def _follow_events(ws: Any) -> int:
    print("继续监听场景事件，Ctrl+C 退出", flush=True)
    async for raw in ws:
        msg = loads(raw)
        kind = msg.get("type")
        if kind == "event":
            print(
                f"event {msg.get('name')}: {json.dumps(msg.get('payload'), ensure_ascii=False)}",
                flush=True,
            )
        elif kind == "peer_status":
            roles = msg.get("roles") or []
            print(f"在线端点: {', '.join(roles) or '(无)'}", flush=True)
        elif kind in ("result", "error"):
            print(json.dumps(msg, ensure_ascii=False, indent=2), flush=True)
    return 0


async def run_agent(uri: str, args: argparse.Namespace) -> int:
    async with websockets.connect(
        uri, open_timeout=5, ping_interval=20, ping_timeout=20, max_size=MAX_FRAME_BYTES
    ) as ws:
        await ws.send(
            dumps(hello("agent", "blenderai-agent", capabilities=["command", "event"]))
        )
        acked = False
        blender_online = False
        pending_id: str | None = None
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except TimeoutError:
                break
            msg = loads(raw)
            kind = msg.get("type")
            if kind == "hello_ack":
                acked = True
                print(f"hub 握手成功 session={msg.get('session_id')} peer={msg.get('peer_id')}", flush=True)
            elif kind == "peer_status":
                roles = msg.get("roles") or []
                print(f"在线端点: {', '.join(roles) or '(无)'}", flush=True)
                blender_online = "blender" in roles
            if acked and blender_online and pending_id is None and kind in ("hello_ack", "peer_status"):
                cmd = command(args.op, _payload_for(args))
                pending_id = cmd["id"]
                await ws.send(dumps(cmd))
                print(f"已发送 {args.op} id={pending_id}", flush=True)
            elif kind == "result" and msg.get("id") == pending_id:
                print(json.dumps(msg, ensure_ascii=False, indent=2), flush=True)
                if not msg.get("ok"):
                    return 1
                if args.listen:
                    return await _follow_events(ws)
                return 0
            elif kind == "event":
                print(f"event {msg.get('name')}: {json.dumps(msg.get('payload'), ensure_ascii=False)}")
            elif kind == "error":
                print(f"error: {msg.get('error')}")
                return 1
        if not acked:
            print("未收到 hello_ack")
            return 1
        if not blender_online:
            print("Blender 未连接。先启用插件，或另开一个终端运行: python -m agent.client --role blender-sim")
            return 1
        print("等待 Blender 结果超时")
        return 1


async def run_blender_sim(uri: str, exit_after: int) -> int:
    answered = 0
    async with websockets.connect(
        uri, open_timeout=5, ping_interval=20, ping_timeout=20, max_size=MAX_FRAME_BYTES
    ) as ws:
        await ws.send(
            dumps(hello("blender", "blender-sim", capabilities=list(BLENDER_OPS)))
        )
        print("blender-sim 已连接，等待指令（Ctrl+C 退出）")
        async for raw in ws:
            msg = loads(raw)
            kind = msg.get("type")
            if kind == "hello_ack":
                print(f"hub 握手成功 session={msg.get('session_id')} peer={msg.get('peer_id')}")
                continue
            if kind == "peer_status":
                roles = msg.get("roles") or []
                print(f"在线端点: {', '.join(roles) or '(无)'}")
                if "agent" in roles and "blender" in roles:
                    print("双向握手完成: agent <-> blender")
                continue
            if kind != "command":
                continue
            cmd_id = msg.get("id")
            op = msg.get("op")
            if not isinstance(cmd_id, str):
                continue
            if op == "ping":
                reply = result(cmd_id, True, {"pong": True, "simulator": True})
            elif op == "get_scene":
                reply = result(
                    cmd_id,
                    True,
                    {
                        "scene": "Simulator",
                        "objects": [],
                        "materials": [],
                        "simulator": True,
                    },
                )
            elif op == "render_preview":
                reply = result(
                    cmd_id,
                    True,
                    {
                        "mime": "image/png",
                        "width": 1,
                        "height": 1,
                        "engine": "WORKBENCH",
                        "byte_length": 70,
                        "image_base64": TINY_PNG_BASE64,
                        "simulator": True,
                    },
                )
            else:
                reply = result(
                    cmd_id,
                    False,
                    error=f"blender-sim 不能执行 {op}。这个指令需要 Blender 插件。",
                )
            await ws.send(dumps(reply))
            print(f"已回复 {op} ok={reply['ok']}")
            answered += 1
            if exit_after and answered >= exit_after:
                print("blender-sim 达到 --exit-after，退出")
                return 0
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="BlenderAI-OS hub client")
    parser.add_argument("--uri", default="ws://127.0.0.1:8765")
    parser.add_argument("--role", choices=("agent", "blender-sim"), default="agent")
    parser.add_argument("--op", choices=BLENDER_OPS, default="ping")
    parser.add_argument("--primitive", choices=("CUBE", "UV_SPHERE", "CYLINDER"), default="CUBE")
    parser.add_argument("--name", default="AIMesh")
    parser.add_argument("--size", type=float, default=1.0)
    parser.add_argument("--x", type=float, default=0.0)
    parser.add_argument("--y", type=float, default=0.0)
    parser.add_argument("--z", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--listen", action="store_true", help="after the command, print scene events until Ctrl+C")
    parser.add_argument("--exit-after", type=int, default=0, help="blender-sim: exit after N replies")
    args = parser.parse_args()
    _utf8_stdio()
    try:
        if args.role == "blender-sim":
            code = asyncio.run(run_blender_sim(args.uri, args.exit_after))
        else:
            code = asyncio.run(run_agent(args.uri, args))
    except KeyboardInterrupt:
        print("已停止")
        code = 0
    except OSError as exc:
        print(f"连接失败 {args.uri}: {exc}")
        print("先启动通信中枢: python -m agent.server")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
