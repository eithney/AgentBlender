"""BlenderAI-OS addon: scene events out, allowlisted ops in.

The addon connects to the hub as the ``blender`` peer. User selection and
object moves become events. The agent sends ``ping``, ``get_scene``,
``create_mesh``, and ``render_preview``. There is no remote ``exec`` of
arbitrary Python.

Install this package (the ``blenderai_os`` folder) as a legacy addon. It
imports ``protocol`` from the repository root, two levels above this file.
"""

import os
import sys
import time
from pathlib import Path

import bpy
from bpy.app.handlers import persistent

bl_info = {
    "name": "BlenderAI-OS",
    "author": "BlenderAI-OS",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > BlenderAI",
    "description": "AI-native scene bridge: WebSocket handshake, scene context, user events",
    "category": "3D View",
}

_IMPORT_ERROR = None
_bridge = None
event = None
result = None
Bridge = None

_MAX_OBJECTS = 400
_MAX_MATERIALS = 64
_MAX_NODES = 24


def _repo_root() -> Path:
    env = os.environ.get("BLENDERAI_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2]


def _bootstrap() -> None:
    root = _repo_root()
    marker = root / "protocol" / "messages.py"
    if not marker.is_file():
        raise ImportError(
            "找不到 protocol 包。请通过仓库内的符号链接启用插件，或设置环境变量 BLENDERAI_ROOT。"
        )
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)


try:
    _bootstrap()
    from . import bridge as _bridge_mod

    Bridge = _bridge_mod.Bridge
    from protocol.messages import event, result
except Exception as exc:
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


class _Runtime:
    def __init__(self) -> None:
        self.executing = False
        self.primed = False
        self.depsgraph_dirty = True
        self.last_selection = None
        self.last_xform = {}
        self.xform_dirty = None


_runtime = _Runtime()


def _round_vec(values, n=3):
    return [round(float(v), 4) for v in list(values)[:n]]


def _loc_key(obj):
    return tuple(_round_vec(obj.location))


def capture_scene(context):
    """Serialize the open scene into a JSON-safe tree for the agent."""
    scene = context.scene
    truncated = False
    objects = []
    source = list(scene.objects)
    if len(source) > _MAX_OBJECTS:
        truncated = True
        source = source[:_MAX_OBJECTS]
    for obj in source:
        try:
            visible = bool(obj.visible_get())
        except Exception:
            visible = not bool(obj.hide_viewport)
        item = {
            "name": obj.name,
            "type": obj.type,
            "location": _round_vec(obj.location),
            "rotation_euler": _round_vec(obj.rotation_euler),
            "scale": _round_vec(obj.scale),
            "parent": obj.parent.name if obj.parent else None,
            "visible": visible,
            "selected": bool(obj.select_get()),
        }
        if obj.type == "LIGHT":
            data = obj.data
            item["light"] = {
                "kind": data.type,
                "energy": round(float(data.energy), 4),
                "color": _round_vec(data.color),
            }
        elif obj.type == "CAMERA":
            data = obj.data
            item["camera"] = {
                "lens": round(float(data.lens), 4),
                "clip_start": round(float(data.clip_start), 4),
                "clip_end": round(float(data.clip_end), 4),
            }
        objects.append(item)

    materials = []
    for mat in bpy.data.materials:
        nodes = []
        tree = getattr(mat, "node_tree", None)
        if tree is not None:
            for node in tree.nodes:
                nodes.append({"name": node.name, "type": node.type})
                if len(nodes) >= _MAX_NODES:
                    break
        materials.append(
            {
                "name": mat.name,
                "use_nodes": tree is not None,
                "nodes": nodes,
            }
        )
        if len(materials) >= _MAX_MATERIALS:
            truncated = True
            break

    camera = scene.camera
    return {
        "scene": scene.name,
        "frame": int(scene.frame_current),
        "render_engine": scene.render.engine,
        "active_camera": camera.name if camera else None,
        "objects": objects,
        "materials": materials,
        "truncated": truncated,
    }


def _location(payload):
    raw = payload.get("location", [0.0, 0.0, 0.0])
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise ValueError("location must be [x, y, z]")
    return (float(raw[0]), float(raw[1]), float(raw[2]))


def create_mesh(payload, context):
    import bmesh

    kind = str(payload.get("primitive") or "CUBE")
    if kind not in ("CUBE", "UV_SPHERE", "CYLINDER"):
        raise ValueError("primitive must be CUBE, UV_SPHERE, or CYLINDER")
    size = float(payload.get("size", 1.0))
    if size < 0.01 or size > 100.0:
        raise ValueError("size must be between 0.01 and 100")
    location = _location(payload)
    name = str(payload.get("name") or "AIMesh").strip()[:60] or "AIMesh"

    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    try:
        if kind == "CUBE":
            bmesh.ops.create_cube(bm, size=size)
        elif kind == "UV_SPHERE":
            bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=8, radius=size)
        else:
            bmesh.ops.create_cone(
                bm,
                cap_ends=True,
                segments=16,
                radius1=size,
                radius2=size,
                depth=size * 2.0,
            )
        bm.to_mesh(mesh)
    finally:
        bm.free()

    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    collection = getattr(context, "collection", None) or context.scene.collection
    collection.objects.link(obj)
    try:
        for item in list(context.selected_objects):
            item.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
    except Exception:
        pass
    return {
        "name": obj.name,
        "type": obj.type,
        "primitive": kind,
        "location": _round_vec(obj.location),
    }


_PREVIEW_ENGINES = {
    "WORKBENCH": ("BLENDER_WORKBENCH",),
    "EEVEE": ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"),
}
_MAX_PREVIEW_BYTES = 400_000


def _assign_engine(scene, engine_key):
    for name in _PREVIEW_ENGINES[engine_key]:
        try:
            scene.render.engine = name
        except TypeError:
            continue
        if scene.render.engine == name:
            return name
    raise ValueError(f"render engine {engine_key} is not available")


def _temporary_camera(scene):
    import mathutils

    data = bpy.data.cameras.new("AI_Preview_Camera")
    obj = bpy.data.objects.new("AI_Preview_Camera", data)
    scene.collection.objects.link(obj)
    obj.location = (4.5, -4.5, 3.2)
    direction = mathutils.Vector((0.0, 0.0, 0.4)) - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    return obj


def _render_still(context):
    try:
        with context.temp_override(scene=context.scene, view_layer=context.view_layer):
            status = bpy.ops.render.render(write_still=True)
    except Exception as exc:
        raise RuntimeError(f"render failed: {exc}") from exc
    if isinstance(status, set) and "FINISHED" not in status:
        raise RuntimeError(f"render ended with {sorted(status)}")


def render_preview(payload, context):
    """Clay (Workbench) or Eevee still, returned as a small PNG."""
    import base64
    import os
    import tempfile

    width = int(payload.get("width", 256))
    height = int(payload.get("height", 256))
    if width < 64 or width > 512 or height < 64 or height > 512:
        raise ValueError("width and height must be between 64 and 512")
    engine_key = str(payload.get("engine") or "WORKBENCH").upper()
    if engine_key not in _PREVIEW_ENGINES:
        raise ValueError("engine must be WORKBENCH or EEVEE")

    scene = context.scene
    render = scene.render
    image = render.image_settings
    shading = scene.display.shading
    eevee = getattr(scene, "eevee", None)
    saved = {
        "engine": render.engine,
        "x": render.resolution_x,
        "y": render.resolution_y,
        "percent": render.resolution_percentage,
        "filepath": render.filepath,
        "format": image.file_format,
        "color": image.color_mode,
        "camera": scene.camera,
        "color_type": shading.color_type,
        "light": shading.light,
        "single_color": tuple(shading.single_color),
        "samples": getattr(eevee, "taa_render_samples", None) if eevee is not None else None,
    }
    temp_camera = None
    fd, path = tempfile.mkstemp(suffix=".png", prefix="blenderai-")
    os.close(fd)
    try:
        active_engine = _assign_engine(scene, engine_key)
        render.resolution_x = width
        render.resolution_y = height
        render.resolution_percentage = 100
        render.filepath = path
        image.file_format = "PNG"
        image.color_mode = "RGB"
        if engine_key == "WORKBENCH":
            shading.light = "STUDIO"
            shading.color_type = "SINGLE"
            shading.single_color = (0.75, 0.75, 0.75)
        elif eevee is not None and saved["samples"] is not None:
            eevee.taa_render_samples = min(int(saved["samples"]), 16)
        if scene.camera is None:
            temp_camera = _temporary_camera(scene)
            scene.camera = temp_camera
        _render_still(context)
        with open(path, "rb") as handle:
            raw = handle.read()
    finally:
        render.engine = saved["engine"]
        render.resolution_x = saved["x"]
        render.resolution_y = saved["y"]
        render.resolution_percentage = saved["percent"]
        render.filepath = saved["filepath"]
        image.file_format = saved["format"]
        image.color_mode = saved["color"]
        shading.color_type = saved["color_type"]
        shading.light = saved["light"]
        shading.single_color = saved["single_color"]
        if eevee is not None and saved["samples"] is not None:
            eevee.taa_render_samples = saved["samples"]
        scene.camera = saved["camera"]
        if temp_camera is not None:
            camera_data = temp_camera.data
            bpy.data.objects.remove(temp_camera, do_unlink=True)
            if camera_data is not None and camera_data.users == 0:
                bpy.data.cameras.remove(camera_data)
        try:
            os.remove(path)
        except OSError:
            pass
    if not raw.startswith(b"\x89PNG"):
        raise ValueError("render did not produce a PNG")
    if len(raw) > _MAX_PREVIEW_BYTES:
        raise ValueError("preview image is too large; use a smaller width and height")
    return {
        "mime": "image/png",
        "width": width,
        "height": height,
        "engine": engine_key,
        "render_engine": active_engine,
        "byte_length": len(raw),
        "image_base64": base64.b64encode(raw).decode("ascii"),
    }


def _dispatch(op, payload, context):
    if op == "ping":
        return {"pong": True, "scene": context.scene.name}
    if op == "get_scene":
        return capture_scene(context)
    if op == "create_mesh":
        return create_mesh(payload, context)
    if op == "render_preview":
        return render_preview(payload, context)
    raise ValueError(f"unknown op: {op}")


def _handle_command(msg):
    cmd_id = msg.get("id")
    op = msg.get("op")
    if not isinstance(cmd_id, str) or not isinstance(op, str) or result is None:
        return
    payload = msg.get("payload") or {}
    _runtime.executing = True
    try:
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        data = _dispatch(op, payload, bpy.context)
        reply = result(cmd_id, True, data)
    except Exception as exc:
        reply = result(cmd_id, False, error=f"{type(exc).__name__}: {exc}")
        print(f"[BlenderAI-OS] {op} failed: {exc}")
    finally:
        _runtime.executing = False
    if _bridge is not None:
        _bridge.send(reply)


def _poll_attention():
    if _bridge is None or not _bridge.ready or _runtime.executing or event is None:
        return
    view_layer = bpy.context.view_layer
    if view_layer is None:
        return
    selected = tuple(sorted(obj.name for obj in view_layer.objects if obj.select_get()))
    active = view_layer.objects.active
    active_name = active.name if active is not None else None
    if not _runtime.primed:
        _runtime.primed = True
        _runtime.last_selection = selected
        if active is not None:
            _runtime.last_xform[active.name] = _loc_key(active)
        _bridge.send(
            event(
                "user_focus",
                {"selected": list(selected), "active": active_name, "reason": "sync"},
            )
        )
        return
    if selected != _runtime.last_selection:
        _runtime.last_selection = selected
        _bridge.send(
            event(
                "user_focus",
                {"selected": list(selected), "active": active_name, "reason": "selection"},
            )
        )
    if active is None:
        return
    key = _loc_key(active)
    prev = _runtime.last_xform.get(active.name)
    now = time.monotonic()
    if prev is None:
        _runtime.last_xform[active.name] = key
        return
    if prev == key:
        return
    dirty = _runtime.xform_dirty
    if dirty is None or dirty[0] != active.name or dirty[1] != key:
        _runtime.xform_dirty = (active.name, key, now)
        return
    if now - dirty[2] < 0.4:
        return
    _runtime.xform_dirty = None
    _runtime.last_xform[active.name] = key
    _bridge.send(event("object_transformed", {"name": active.name, "location": list(key)}))


def _tag_redraw():
    wm = bpy.context.window_manager
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _drain_inbox():
    if _bridge is None:
        return
    wm = bpy.context.window_manager
    changed = False
    for msg in _bridge.poll_inbox():
        kind = msg.get("type")
        if kind == "local_status":
            state = msg.get("state")
            detail = str(msg.get("detail") or "")
            if state == "connecting":
                wm.blenderai_status = "正在连接"
                wm.blenderai_detail = detail
            elif state == "retry":
                wm.blenderai_status = "重试中"
                wm.blenderai_detail = detail[:180]
                _runtime.primed = False
            elif state == "disconnected":
                wm.blenderai_status = "未连接"
                wm.blenderai_detail = ""
                _runtime.primed = False
            changed = True
        elif kind == "hello_ack":
            wm.blenderai_status = "已连接中枢"
            wm.blenderai_session = str(msg.get("session_id") or "")
            wm.blenderai_detail = ""
            changed = True
        elif kind == "peer_status":
            roles = msg.get("roles") or []
            wm.blenderai_peers = ", ".join(roles)
            if "agent" in roles:
                wm.blenderai_status = "Agent 已握手"
            changed = True
        elif kind == "command":
            _handle_command(msg)
        elif kind == "error":
            wm.blenderai_detail = str(msg.get("error") or "")
            changed = True
    if changed:
        _tag_redraw()


def _on_timer():
    try:
        if _bridge is not None:
            _drain_inbox()
            if _bridge.ready and (_runtime.depsgraph_dirty or not _runtime.primed):
                _runtime.depsgraph_dirty = False
                _poll_attention()
    except Exception as exc:
        print(f"[BlenderAI-OS] timer error: {exc}")
    return 0.1


@persistent
def _on_depsgraph_update(scene, depsgraph):
    _runtime.depsgraph_dirty = True


@persistent
def _on_load_post(_dummy):
    _runtime.primed = False
    _runtime.last_selection = None
    _runtime.last_xform.clear()
    _runtime.xform_dirty = None
    _runtime.depsgraph_dirty = True


def _uri_from_prefs():
    addon = bpy.context.preferences.addons.get(__package__ or "")
    if addon is None:
        return "ws://127.0.0.1:8765"
    prefs = addon.preferences
    return f"ws://{prefs.host}:{int(prefs.port)}"


class BlenderAIPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    host: bpy.props.StringProperty(name="Host", default="127.0.0.1")
    port: bpy.props.IntProperty(name="Port", default=8765, min=1, max=65535)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.prop(self, "host")
        col.prop(self, "port")


class BLENDERAI_OT_reconnect(bpy.types.Operator):
    bl_idname = "blenderai.reconnect"
    bl_label = "重新连接"
    bl_description = "重新连接 BlenderAI-OS 通信中枢"

    def execute(self, context):
        global _bridge
        if _IMPORT_ERROR or Bridge is None:
            self.report({"ERROR"}, _IMPORT_ERROR or "bridge 不可用")
            return {"CANCELLED"}
        if _bridge is not None:
            _bridge.stop()
        _bridge = Bridge()
        _runtime.primed = False
        _bridge.start(_uri_from_prefs())
        return {"FINISHED"}


class BLENDERAI_PT_main(bpy.types.Panel):
    bl_label = "BlenderAI-OS"
    bl_idname = "BLENDERAI_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BlenderAI"

    def draw(self, context):
        layout = self.layout
        if _IMPORT_ERROR:
            col = layout.column(align=True)
            col.label(text="插件依赖未就绪", icon="ERROR")
            for chunk_start in range(0, min(len(_IMPORT_ERROR), 240), 80):
                col.label(text=_IMPORT_ERROR[chunk_start : chunk_start + 80])
            return
        wm = context.window_manager
        layout.label(text=f"状态: {wm.blenderai_status}")
        layout.label(text=f"会话: {wm.blenderai_session or '-'}")
        layout.label(text=f"端点: {wm.blenderai_peers or '-'}")
        if wm.blenderai_detail:
            layout.label(text=wm.blenderai_detail[:80])
        layout.operator("blenderai.reconnect", icon="FILE_REFRESH")


_CLASSES = (BlenderAIPreferences, BLENDERAI_OT_reconnect, BLENDERAI_PT_main)


def register():
    global _bridge
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    wm = bpy.types.WindowManager
    wm.blenderai_status = bpy.props.StringProperty(default="未连接")
    wm.blenderai_session = bpy.props.StringProperty(default="")
    wm.blenderai_peers = bpy.props.StringProperty(default="")
    wm.blenderai_detail = bpy.props.StringProperty(default="")
    if Bridge is None:
        print(f"[BlenderAI-OS] 未启动连接: {_IMPORT_ERROR}")
        return
    _bridge = Bridge()
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    if not bpy.app.timers.is_registered(_on_timer):
        bpy.app.timers.register(_on_timer, persistent=True)
    _bridge.start(_uri_from_prefs())
    print("[BlenderAI-OS] bridge started")


def unregister():
    global _bridge
    if _bridge is not None:
        _bridge.stop()
        _bridge = None
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    if bpy.app.timers.is_registered(_on_timer):
        bpy.app.timers.unregister(_on_timer)
    wm = bpy.types.WindowManager
    for prop in ("blenderai_status", "blenderai_session", "blenderai_peers", "blenderai_detail"):
        if hasattr(wm, prop):
            delattr(wm, prop)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
