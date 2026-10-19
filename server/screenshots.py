"""Screenshot helpers using mss + Pillow. Read-only."""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from typing import Tuple

import mss
from PIL import Image


@dataclass
class ScreenshotResult:
    path: str
    width: int
    height: int
    bytes_jpeg: int


def _save_jpeg_resized(
    img: Image.Image, max_long_edge: int = 1280, quality: int = 60
) -> Tuple[str, bytes, Tuple[int, int]]:
    """Resize so longest edge ≤ max_long_edge, save to JPEG bytes."""
    w, h = img.size
    long_edge = max(w, h)
    if long_edge > max_long_edge:
        scale = max_long_edge / long_edge
        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        img = img.resize(new_size, Image.LANCZOS)
    out_path = os.path.join(
        tempfile.gettempdir(), f"wl_screenshot_{uuid.uuid4().hex}.jpg"
    )
    img.save(out_path, format="JPEG", quality=quality, optimize=True)
    with open(out_path, "rb") as f:
        data = f.read()
    return out_path, data, img.size


def _grab_image(grab_callable) -> Image.Image:
    """Call `grab_callable(sct)` and return an RGB PIL Image."""
    with mss.MSS() as sct:
        shot = grab_callable(sct)
        return Image.frombytes("RGB", shot.size, shot.rgb)


def screenshot_screen() -> ScreenshotResult:
    def _grab(sct):
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        return sct.grab(monitor)

    img = _grab_image(_grab)
    path, data, (w, h) = _save_jpeg_resized(img)
    return ScreenshotResult(path=path, width=w, height=h, bytes_jpeg=len(data))


def screenshot_region(x: int, y: int, w: int, h: int) -> ScreenshotResult:
    if w <= 0 or h <= 0:
        raise ValueError("region width/height must be > 0")

    def _grab(sct):
        return sct.grab({"left": int(x), "top": int(y), "width": int(w), "height": int(h)})

    img = _grab_image(_grab)
    path, data, (nw, nh) = _save_jpeg_resized(img)
    return ScreenshotResult(path=path, width=nw, height=nh, bytes_jpeg=len(data))