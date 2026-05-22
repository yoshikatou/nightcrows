"""PC シーン実行エンジン。

PC シーン JSON を読み込み、各ステップを順次実行する:
- snapshot:   Win32 キャプチャ + cv2.matchTemplate でテンプレート一致を待つ
- tap:        PicoMouse.click()（rx/ry = ウィンドウクライアント相対比率 0.0〜1.0）
- swipe:      PicoMouse.press() + move_to() + release()
- wait_fixed: time.sleep()
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable

import cv2
import win32gui

from .capture import capture_window
from .window_picker import find_hwnd_by_title

LogFn = Callable[[str], None]
StopFn = Callable[[], bool]
StepCb = Callable[[int, int], None]   # (current_step, total_steps)

SCENES_DIR = "scenes"


# ----------------------------------------------------------------- データモデル
@dataclass
class PcStep:
    type: str
    params: dict = field(default_factory=dict)


@dataclass
class PcScene:
    name: str = "untitled"
    window_title: str = ""
    steps: list[PcStep] = field(default_factory=list)


# ----------------------------------------------------------------- JSON 入出力
def load_pc_scene(path: str) -> PcScene:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    steps: list[PcStep] = []
    for s in data.get("steps", []):
        s = dict(s)
        t = s.pop("type")
        steps.append(PcStep(type=t, params=s))
    return PcScene(
        name=data.get("name", "untitled"),
        window_title=data.get("window_title", ""),
        steps=steps,
    )


def save_pc_scene(scene: PcScene, path: str) -> None:
    data = {
        "name": scene.name,
        "window_title": scene.window_title,
        "steps": [{"type": s.type, **s.params} for s in scene.steps],
    }
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ----------------------------------------------------------------- 座標変換
def rel_to_abs(hwnd: int, rx: float, ry: float) -> tuple[int, int]:
    """ウィンドウ相対比率 (0.0〜1.0) を絶対スクリーン座標に変換する。"""
    rect = win32gui.GetClientRect(hwnd)       # (0, 0, width, height)
    w, h = rect[2], rect[3]
    origin = win32gui.ClientToScreen(hwnd, (0, 0))
    return int(origin[0] + rx * w), int(origin[1] + ry * h)


# ----------------------------------------------------------------- シーン実行
def run_pc_scene(
    scene: PcScene,
    mouse,                              # PicoMouse | None
    hwnd: int | None = None,
    log: LogFn = print,
    should_stop: StopFn = lambda: False,
    step_callback: StepCb | None = None,
) -> bool:
    """シーンを実行する。全ステップ完了なら True、中断/失敗なら False。"""
    if hwnd is None and scene.window_title:
        hwnd = find_hwnd_by_title(scene.window_title)

    total = len(scene.steps)
    log(f"シーン開始: {scene.name}  ({total} ステップ)")

    for i, step in enumerate(scene.steps):
        if should_stop():
            log(f"  中断 (ステップ {i + 1}/{total})")
            return False

        if step_callback:
            step_callback(i + 1, total)

        t = step.type
        p = step.params

        if t == "wait_fixed":
            secs = float(p.get("seconds", 1.0))
            log(f"  [{i+1}/{total}] wait_fixed {secs}s")
            deadline = time.monotonic() + secs
            while time.monotonic() < deadline:
                if should_stop():
                    return False
                time.sleep(0.05)

        elif t == "snapshot":
            tmpl_path = p.get("path", "")
            timeout_s = float(p.get("timeout_s", 10.0))
            threshold = float(p.get("threshold", 0.85))
            log(f"  [{i+1}/{total}] snapshot {tmpl_path}  timeout={timeout_s}s")

            tmpl = cv2.imread(tmpl_path, cv2.IMREAD_COLOR)
            if tmpl is None:
                log(f"    エラー: テンプレート画像が読めません: {tmpl_path}")
                return False

            matched = False
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if should_stop():
                    return False
                if hwnd and win32gui.IsWindow(hwnd):
                    img = capture_window(hwnd)
                    if (img is not None
                            and img.shape[0] >= tmpl.shape[0]
                            and img.shape[1] >= tmpl.shape[1]):
                        res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
                        _, maxv, _, _ = cv2.minMaxLoc(res)
                        if maxv >= threshold:
                            log(f"    一致 score={maxv:.3f}")
                            matched = True
                            break
                time.sleep(0.5)

            if not matched:
                log(f"    タイムアウト: {tmpl_path}")
                return False

        elif t == "tap":
            rx = float(p.get("rx", 0.5))
            ry = float(p.get("ry", 0.5))
            button = str(p.get("button", "L")).upper()
            hold_ms = int(p.get("duration_ms", 50))
            log(f"  [{i+1}/{total}] tap rx={rx:.3f} ry={ry:.3f}")

            if not _check_hwnd(hwnd, log) or not _check_mouse(mouse, log):
                continue

            ax, ay = rel_to_abs(hwnd, rx, ry)
            mouse.click(ax, ay, button, hold_ms=hold_ms)

        elif t == "swipe":
            rx1 = float(p.get("rx1", 0.5))
            ry1 = float(p.get("ry1", 0.5))
            rx2 = float(p.get("rx2", 0.5))
            ry2 = float(p.get("ry2", 0.5))
            duration_ms = int(p.get("duration_ms", 500))
            log(f"  [{i+1}/{total}] swipe ({rx1:.3f},{ry1:.3f}) -> ({rx2:.3f},{ry2:.3f})")

            if not _check_hwnd(hwnd, log) or not _check_mouse(mouse, log):
                continue

            x1, y1 = rel_to_abs(hwnd, rx1, ry1)
            x2, y2 = rel_to_abs(hwnd, rx2, ry2)
            dist = max(1, max(abs(x2 - x1), abs(y2 - y1)))
            n_steps = max(5, dist // 15)
            step_delay = max(0.01, duration_ms / 1000.0 / n_steps)
            max_step = max(1, dist // n_steps + 1)

            mouse.move_cursor(x1, y1)
            time.sleep(0.05)
            mouse.press("L")
            mouse.move_to(x2, y2, max_step=max_step, delay=step_delay)
            mouse.release()

        else:
            log(f"  [{i+1}/{total}] 不明なステップタイプ: {t!r} — スキップ")

        if should_stop():
            return False

    if step_callback:
        step_callback(total, total)
    log(f"シーン完了: {scene.name}")
    return True


def _check_hwnd(hwnd: int | None, log: LogFn) -> bool:
    if not hwnd or not win32gui.IsWindow(hwnd):
        log("    ウィンドウが見つかりません — スキップ")
        return False
    return True


def _check_mouse(mouse, log: LogFn) -> bool:
    if mouse is None:
        log("    Pico 未接続 — スキップ")
        return False
    return True
