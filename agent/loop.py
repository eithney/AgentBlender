"""Async agent loop: observe the scene, call one tool, reflect, repeat.

The planner is a small heuristic so the cycle can run without an API key.
``HeuristicPlanner.choose`` is the tool-calling seam; a model can replace it
later and keep the same observe / act / reflect cycle.

    python -m agent.loop --goal "查看当前场景"
    python -m agent.loop --goal "创建一个名为 Cup 的球体"
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.preview import decode_preview, inspect_png, save_preview
from agent.scene_context import format_scene, object_names
from agent.session import HubSession

_ROOT = Path(__file__).resolve().parents[1]
_PREVIEW_DIR = _ROOT / "previews"

_CREATE_WORDS = ("创建", "生成", "添加", "create", "add")


@dataclass
class Action:
    op: str
    payload: dict[str, Any]
    reason: str


@dataclass
class Note:
    op: str
    ok: bool
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class LoopResult:
    code: int
    conclusion: str
    notes: list[Note]


def parse_creation(goal: str) -> dict[str, Any] | None:
    text = goal.lower()
    primitive: str | None = None
    if any(word in text for word in ("球", "sphere")):
        primitive = "UV_SPHERE"
    elif any(word in text for word in ("柱", "cylinder")):
        primitive = "CYLINDER"
    elif any(word in text for word in ("立方", "方块", "cube", "盒子")):
        primitive = "CUBE"
    elif any(word in goal for word in ("杯",)) or "cup" in text:
        primitive = "UV_SPHERE"
    elif any(word in text for word in _CREATE_WORDS):
        primitive = "CUBE"
    if primitive is None:
        return None
    match = re.search(r"(?:名为|叫做|named)\s*([A-Za-z0-9_\u4e00-\u9fff-]+)", goal, re.IGNORECASE)
    if match:
        name = match.group(1)
    elif "杯" in goal or "cup" in text:
        name = "Cup"
    else:
        name = "AIMesh"
    return {"primitive": primitive, "name": name, "size": 1.0, "location": [0.0, 0.0, 0.0]}


def wants_preview(goal: str) -> bool:
    text = goal.lower()
    return any(word in goal for word in ("预览", "渲染", "看看")) or any(
        word in text for word in ("preview", "render")
    )


def _scene_ready(notes: list[Note]) -> bool:
    return any(note.op == "get_scene" and note.ok for note in notes)


def _creation_status(notes: list[Note], name: str) -> str:
    seen_create = False
    found: bool | None = None
    for note in notes:
        if note.op == "create_mesh" and note.ok:
            seen_create = True
            found = None
        elif seen_create and note.op == "get_scene" and note.ok:
            found = name in object_names(note.payload)
    if not seen_create:
        return "no-create"
    if found is None:
        return "need-check"
    return "present" if found else "absent"


class HeuristicPlanner:
    """Pick the next allowlisted op from the goal and the notes so far."""

    def __init__(self, goal: str) -> None:
        self.goal = goal
        self.intent = parse_creation(goal)

    def choose(self, notes: list[Note]) -> Action | None:
        if not _scene_ready(notes):
            if any(note.op == "get_scene" and not note.ok for note in notes):
                return None
            return Action("get_scene", {}, "先读取场景")
        if self.intent is not None:
            creates = [note for note in notes if note.op == "create_mesh"]
            if not creates:
                return Action("create_mesh", dict(self.intent), "按目标创建网格")
            if not creates[-1].ok:
                return None
            status = _creation_status(notes, str(self.intent["name"]))
            if status == "need-check":
                return Action("get_scene", {}, "创建后复核场景")
            if status != "present":
                return None
        if _should_render(self.goal, self.intent is not None):
            renders = [note for note in notes if note.op == "render_preview"]
            if not renders:
                return _preview_action("WORKBENCH", "渲染黏土预览")
            last = renders[-1]
            if last.ok and last.payload.get("accepted"):
                return None
            if len(renders) >= 2:
                return None
            used = str(last.payload.get("engine") or "WORKBENCH").upper()
            nxt = "EEVEE" if used == "WORKBENCH" else "WORKBENCH"
            return _preview_action(nxt, f"预览未通过，改用 {nxt} 再渲染")
        return None


def _should_render(goal: str, created: bool) -> bool:
    return created or wants_preview(goal)


def _preview_action(engine: str, reason: str) -> Action:
    return Action(
        "render_preview",
        {"engine": engine, "width": 256, "height": 256},
        reason,
    )


def reflect(op: str, result: dict[str, Any], intent: dict[str, Any] | None, created_ok: bool) -> str:
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if not result.get("ok"):
        return f"{op} 失败: {result.get('error') or '未知错误'}"
    if op == "get_scene":
        names = object_names(payload)
        if created_ok and intent is not None:
            target = str(intent["name"])
            if target in names:
                return f"复核通过，场景中有 {target}"
            return f"复核未在场景中找到 {target}"
        return f"已读取场景 {payload.get('scene') or '?'}，物体 {len(names)} 个"
    if op == "create_mesh":
        name = payload.get("name") or (intent or {}).get("name") or "?"
        primitive = payload.get("primitive") or (intent or {}).get("primitive") or ""
        return f"已创建 {name}（{primitive}）"
    if op == "render_preview":
        return f"已收到预览图 {payload.get('width')}x{payload.get('height')}"
    return f"{op} 完成"


def conclude(notes: list[Note]) -> str:
    if not notes:
        return "没有执行任何指令"
    failed = [note for note in notes if not note.ok]
    if failed:
        return failed[-1].summary
    return notes[-1].summary


async def run_goal(uri: str, goal: str, timeout: float = 30.0, max_steps: int = 6) -> LoopResult:
    planner = HeuristicPlanner(goal)
    notes: list[Note] = []
    print(f"目标: {goal}", flush=True)
    async with HubSession(uri, timeout) as session:
        print(f"hub 握手成功 session={session.session_id}", flush=True)
        steps = 0
        while steps < max_steps:
            action = planner.choose(notes)
            if action is None:
                break
            steps += 1
            before_events = len(session.events)
            print(f"[{steps}] {action.op} — {action.reason}", flush=True)
            result = await session.request(action.op, action.payload)
            for event in session.events[before_events:]:
                print(f"    事件 {event.get('name')}: {event.get('payload')}", flush=True)
            created_ok = any(note.op == "create_mesh" and note.ok for note in notes)
            summary = reflect(action.op, result, planner.intent, created_ok)
            payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
            stored = {key: value for key, value in payload.items() if key != "image_base64"}
            if action.payload.get("engine"):
                stored["engine"] = action.payload["engine"]
            note_ok = bool(result.get("ok"))
            note_error = result.get("error") if isinstance(result.get("error"), str) else None
            if action.op == "render_preview" and note_ok:
                raw = decode_preview(payload)
                verdict = inspect_png(raw)
                stored["accepted"] = verdict.ok
                stored["width"] = verdict.width
                stored["height"] = verdict.height
                if verdict.ok:
                    path = save_preview(raw, _PREVIEW_DIR)
                    summary = f"预览通过验收 {verdict.width}x{verdict.height}，保存到 {path}"
                    stored["saved_to"] = str(path)
                else:
                    summary = f"预览未通过验收: {verdict.reason}"
                    note_ok = False
                    note_error = verdict.reason
            note = Note(
                op=action.op,
                ok=note_ok,
                summary=summary,
                payload=stored,
                error=note_error,
            )
            notes.append(note)
            print(f"    反思: {summary}", flush=True)
            if action.op == "get_scene" and note.ok:
                print(format_scene(payload), flush=True)
        else:
            conclusion = "达到最大步数，停止"
            print(f"结论: {conclusion}", flush=True)
            return LoopResult(1, conclusion, notes)
    conclusion = conclude(notes)
    print(f"结论: {conclusion}", flush=True)
    return LoopResult(0, conclusion, notes)


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="BlenderAI-OS agent loop")
    parser.add_argument("--uri", default="ws://127.0.0.1:8765")
    parser.add_argument("--goal", default="查看当前场景")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-steps", type=int, default=6)
    args = parser.parse_args()
    _utf8_stdio()
    try:
        outcome = asyncio.run(run_goal(args.uri, args.goal, args.timeout, args.max_steps))
        code = outcome.code
    except KeyboardInterrupt:
        print("已停止")
        code = 0
    except (OSError, TimeoutError, RuntimeError) as exc:
        print(f"循环未完成: {exc}")
        print("先启动通信中枢和 Blender 插件，或另开终端运行: python -m agent.client --role blender-sim")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
