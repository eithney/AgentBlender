"""Decide whether a preview PNG is worth delivering.

The check is structural: a real Workbench frame is at least 64 pixels on a
side and heavier than a placeholder. It does not judge composition. A failed
check is what sends the loop back for one more render.
"""

from __future__ import annotations

import base64
import struct
import time
from dataclasses import dataclass
from pathlib import Path

_MIN_EDGE = 64


@dataclass(frozen=True)
class PreviewVerdict:
    ok: bool
    reason: str
    width: int
    height: int


def inspect_png(raw: bytes) -> PreviewVerdict:
    if len(raw) < 24 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return PreviewVerdict(False, "不是有效 PNG", 0, 0)
    width, height = struct.unpack(">II", raw[16:24])
    if width < _MIN_EDGE or height < _MIN_EDGE:
        return PreviewVerdict(False, f"预览过小 {width}x{height}", width, height)
    return PreviewVerdict(True, f"预览可用 {width}x{height}", width, height)


def decode_preview(payload: dict) -> bytes:
    encoded = payload.get("image_base64")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("预览结果缺少 image_base64")
    return base64.b64decode(encoded, validate=True)


def save_preview(raw: bytes, directory: Path) -> Path:
    directory.mkdir(exist_ok=True)
    path = directory / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}.png"
    path.write_bytes(raw)
    return path
