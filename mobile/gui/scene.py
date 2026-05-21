"""シーン/ステップのデータモデルと JSON 入出力。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass
class Step:
    type: str  # "tap" | "swipe" | "wait_fixed" | "wait_image"
    params: dict = field(default_factory=dict)


@dataclass
class Scene:
    name: str = "untitled"
    device_ip: str = ""
    rotation: int = 0
    phys_size: tuple[int, int] = (1220, 2712)
    logical_size: tuple[int, int] = (1220, 2712)
    steps: list[Step] = field(default_factory=list)


def _extract_ip(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    return raw.rsplit(":", 1)[0] if ":" in raw else raw


def save_scene(scene: Scene, path: str) -> None:
    data = {
        "name": scene.name,
        "device_ip": scene.device_ip,
        "rotation": scene.rotation,
        "phys_size": list(scene.phys_size),
        "logical_size": list(scene.logical_size),
        "steps": [{"type": s.type, **s.params} for s in scene.steps],
    }
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_scene(path: str) -> Scene:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    steps: list[Step] = []
    for s in data.get("steps", []):
        s = dict(s)
        t = s.pop("type")
        steps.append(Step(type=t, params=s))
    if "device_ip" in data:
        device_ip = str(data.get("device_ip") or "").strip()
    else:
        device_ip = _extract_ip(str(data.get("serial") or ""))
    return Scene(
        name=data.get("name", "untitled"),
        device_ip=device_ip,
        rotation=data.get("rotation", 0),
        phys_size=tuple(data.get("phys_size", [1220, 2712])),
        logical_size=tuple(data.get("logical_size", [1220, 2712])),
        steps=steps,
    )
