"""Turn a get_scene payload into the text the agent reads each turn."""

from __future__ import annotations

from typing import Any


def format_scene(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "（没有场景数据）"
    lines = [f"# 场景 {payload.get('scene') or '?'}"]
    if payload.get("simulator"):
        lines.append("来源: blender-sim，没有真实 bpy 数据")
    if payload.get("frame") is not None:
        lines.append(f"帧: {payload.get('frame')}")
    if payload.get("render_engine"):
        lines.append(f"渲染引擎: {payload.get('render_engine')}")
    if "active_camera" in payload:
        lines.append(f"相机: {payload.get('active_camera') or '无'}")

    objects = payload.get("objects") or []
    lines.append(f"物体: {len(objects)}")
    if not objects:
        lines.append("- （无物体）")
    else:
        for obj in objects[:80]:
            lines.append(_object_line(obj))
        if len(objects) > 80:
            lines.append(f"- … 其余 {len(objects) - 80} 个已省略")

    materials = payload.get("materials") or []
    if materials:
        lines.append(f"材质: {len(materials)}")
        for mat in materials[:24]:
            nodes = [str(node.get("type") or "") for node in (mat.get("nodes") or [])]
            node_text = ", ".join(node for node in nodes if node) or "无节点"
            lines.append(f"- {mat.get('name')} [{node_text}]")
    if payload.get("truncated"):
        lines.append("场景树已截断")
    return "\n".join(lines)


def object_names(payload: dict[str, Any] | None) -> list[str]:
    if not payload:
        return []
    names: list[str] = []
    for obj in payload.get("objects") or []:
        name = obj.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def _object_line(obj: dict[str, Any]) -> str:
    kind = obj.get("type") or "?"
    loc = obj.get("location")
    flags = []
    if obj.get("selected"):
        flags.append("选中")
    if obj.get("parent"):
        flags.append(f"父级={obj.get('parent')}")
    if isinstance(obj.get("light"), dict):
        flags.append(f"灯光={obj['light'].get('kind')}")
    if isinstance(obj.get("camera"), dict):
        flags.append(f"焦距={obj['camera'].get('lens')}")
    suffix = f" {', '.join(flags)}" if flags else ""
    return f"- {obj.get('name')} [{kind}] loc={loc}{suffix}"
