"""シーンの再生エンジン。"""
from __future__ import annotations

import random
import time
from typing import Callable

import cv2
import numpy as np

from .adb import input_swipe, input_keyevent, screencap
from .scene import Scene, Step, load_scene


LogFn = Callable[[str], None]
StopFn = Callable[[], bool]
StepFn = Callable[[int], None]   # 現在実行中のステップインデックスを通知するコールバック


_MAX_CALL_DEPTH = 10


def replay_scene(scene: Scene, serial: str,
                 log: LogFn = print,
                 should_stop: StopFn = lambda: False,
                 on_step: StepFn | None = None,
                 _depth: int = 0,
                 _seq_state: dict | None = None) -> None:
    if _seq_state is None:
        _seq_state = {}
    for i, step in enumerate(scene.steps):
        if should_stop():
            log("中断")
            return
        if on_step is not None and _depth == 0:
            on_step(i)
        log(f"[{i + 1}/{len(scene.steps)}] {step.type} {step.params}")

        if step.type == "tap":
            p = step.params
            input_swipe(serial, p["x"], p["y"], p["x"], p["y"],
                        int(p.get("duration_ms", 100)))
        elif step.type == "swipe":
            p = step.params
            input_swipe(serial, p["x1"], p["y1"], p["x2"], p["y2"],
                        int(p.get("duration_ms", 500)))
        elif step.type == "scroll":
            _do_scroll(serial, step, log)
        elif step.type == "wait_fixed":
            _interruptible_sleep(float(step.params.get("seconds", 1.0)), should_stop)
        elif step.type == "wait_image":
            if not _wait_image(serial, step, log, should_stop):
                log("wait_image タイムアウト - 中断")
                return
        elif step.type == "tap_image":
            if not _tap_image(serial, step, log, should_stop):
                log("tap_image タイムアウト - 中断")
                return
        elif step.type == "if_image":
            _do_if_image(step, serial, log, should_stop, _depth, _seq_state)
        elif step.type == "keyevent":
            input_keyevent(serial, step.params.get("keycode", "KEYCODE_BACK"))
        elif step.type in ("snapshot", "group_header"):
            pass
        elif step.type == "call_scene":
            _do_call_scene(step, serial, log, should_stop, _depth, _seq_state)
        elif step.type == "pick_scene":
            _do_pick_scene(step, serial, log, should_stop, _depth, _seq_state)
        else:
            log(f"未知のステップ型: {step.type}")
    log("完了")


def _do_call_scene(step: Step, serial: str, log: LogFn,
                   should_stop: StopFn, depth: int,
                   seq_state: dict | None = None) -> None:
    sub_path = step.params.get("scene", "").strip()
    if not sub_path:
        log("call_scene: scene パスが空")
        return
    if depth >= _MAX_CALL_DEPTH:
        log(f"call_scene: 最大深度 ({_MAX_CALL_DEPTH}) に達したためスキップ: {sub_path}")
        return
    try:
        sub = load_scene(sub_path)
        log(f"  → サブシーン [{sub.name}]")
        replay_scene(sub, serial, log=log, should_stop=should_stop,
                     _depth=depth + 1, _seq_state=seq_state)
    except Exception as e:
        log(f"  サブシーン読込失敗: {sub_path}: {e}")


def _do_pick_scene(step: Step, serial: str, log: LogFn,
                   should_stop: StopFn, depth: int,
                   seq_state: dict) -> None:
    """pick_scene: ランダム or 順番でシーンを1つ選んで実行する。"""
    p = step.params
    mode = p.get("mode", "random")
    scenes: list[str] = p.get("scenes") or []
    if not scenes:
        log("pick_scene: シーンリストが空")
        return
    if depth >= _MAX_CALL_DEPTH:
        log(f"pick_scene: 最大深度に達したためスキップ")
        return

    if mode == "sequential":
        step_id = p.get("step_id", "")
        idx = seq_state.get(step_id, 0) % len(scenes)
        seq_state[step_id] = (idx + 1) % len(scenes)
        chosen = scenes[idx]
        log(f"  pick_scene [順番 {idx + 1}/{len(scenes)}]: {chosen}")
    else:
        chosen = random.choice(scenes)
        log(f"  pick_scene [ランダム {len(scenes)}択]: {chosen}")

    try:
        sub = load_scene(chosen)
        log(f"  → [{sub.name}]")
        replay_scene(sub, serial, log=log, should_stop=should_stop,
                     _depth=depth + 1, _seq_state=seq_state)
    except Exception as e:
        log(f"  pick_scene シーン読込失敗: {chosen}: {e}")


def _jitter(base: int, jitter: int) -> int:
    if jitter <= 0:
        return base
    return base + random.randint(-jitter, jitter)


def _do_scroll(serial: str, step: Step, log: LogFn) -> None:
    p = step.params
    x1 = _jitter(int(p["x1"]), int(p.get("x1_jitter", 0)))
    y1 = _jitter(int(p["y1"]), int(p.get("y1_jitter", 0)))
    x2 = _jitter(int(p["x2"]), int(p.get("x2_jitter", 0)))
    y2 = _jitter(int(p["y2"]), int(p.get("y2_jitter", 0)))
    dur = max(100, _jitter(int(p.get("duration_ms", 10000)),
                           int(p.get("duration_jitter_ms", 0))))
    log(f"  scroll jittered: ({x1},{y1})->({x2},{y2}) {dur}ms")
    input_swipe(serial, x1, y1, x2, y2, dur)


def _interruptible_sleep(seconds: float, should_stop: StopFn) -> None:
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        if should_stop():
            return
        time.sleep(min(0.1, t_end - time.monotonic()))


def _check_image_now(serial: str, p: dict, log: LogFn) -> bool:
    """1回だけスクリーンキャプチャしてテンプレートマッチを行い結果を返す。"""
    tmpl = cv2.imread(p.get("template", ""), cv2.IMREAD_COLOR)
    if tmpl is None:
        return False
    try:
        png = screencap(serial)
    except Exception:
        return False
    arr = np.frombuffer(png, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return False
    region = p.get("region")
    target, rx, ry = img, 0, 0
    if region and len(region) == 4:
        x, y, w, h = region
        h_img, w_img = img.shape[:2]
        rx, ry = max(0, x), max(0, y)
        target = img[ry:min(y + h, h_img), rx:min(x + w, w_img)]
    if target.shape[0] < tmpl.shape[0] or target.shape[1] < tmpl.shape[1]:
        return False
    res = cv2.matchTemplate(target, tmpl, cv2.TM_CCOEFF_NORMED)
    _, maxv, _, _ = cv2.minMaxLoc(res)
    threshold = float(p.get("threshold", 0.85))
    log(f"  if_image score: {maxv:.3f} (threshold={threshold})")
    return maxv >= threshold


def _do_if_image(step: Step, serial: str, log: LogFn,
                 should_stop: StopFn, depth: int,
                 seq_state: dict | None = None) -> None:
    """if_image: マッチ結果に応じて then/else ブランチを実行する。"""
    if depth >= _MAX_CALL_DEPTH:
        log(f"  if_image: 最大深度に達したためスキップ")
        return
    matched = _check_image_now(serial, step.params, log)
    label = "then" if matched else "else"

    # インラインステップ（新形式）を優先
    inline: list[dict] = step.params.get("then_steps" if matched else "else_steps") or []
    if inline:
        branch = [Step(type=d["type"], params=dict(d.get("params", {}))) for d in inline]
        from .scene import Scene as _Scene
        tmp = _Scene(name=f"_if_{label}_", steps=branch)
        log(f"  if_image → {label}: {len(branch)} ステップ実行")
        replay_scene(tmp, serial, log=log, should_stop=should_stop,
                     _depth=depth + 1, _seq_state=seq_state)
        return

    # シーン参照（後方互換）
    sub_path = step.params.get("then_scene" if matched else "else_scene", "").strip()
    if not sub_path:
        log(f"  if_image → {label}: ステップなし（スキップ）")
        return
    try:
        sub = load_scene(sub_path)
        log(f"  if_image → {label} scene: [{sub.name}]")
        replay_scene(sub, serial, log=log, should_stop=should_stop,
                     _depth=depth + 1, _seq_state=seq_state)
    except Exception as e:
        log(f"  if_image サブシーン読込失敗: {sub_path}: {e}")


def _tap_image(serial: str, step: Step, log: LogFn, should_stop: StopFn) -> bool:
    """テンプレートが画面に現れたらその中心をタップして True を返す。タイムアウトなら False。"""
    p = step.params
    template_path = p["template"]
    threshold = float(p.get("threshold", 0.85))
    timeout_s = float(p.get("timeout_s", 30))
    region = p.get("region")
    offset_x = int(p.get("tap_offset_x", 0))
    offset_y = int(p.get("tap_offset_y", 0))

    tmpl = cv2.imread(template_path, cv2.IMREAD_COLOR)
    if tmpl is None:
        log(f"テンプレート読み込み失敗: {template_path}")
        return False

    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if should_stop():
            return False
        try:
            png = screencap(serial)
        except Exception as e:
            log(f"  screencap エラー: {e}")
            time.sleep(0.5)
            continue
        arr = np.frombuffer(png, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            time.sleep(0.3)
            continue

        rx, ry = 0, 0
        target = img
        if region and len(region) == 4:
            x, y, w, h = region
            h_img, w_img = img.shape[:2]
            rx, ry = max(0, x), max(0, y)
            x2, y2 = min(x + w, w_img), min(y + h, h_img)
            target = img[ry:y2, rx:x2]

        if target.shape[0] < tmpl.shape[0] or target.shape[1] < tmpl.shape[1]:
            time.sleep(0.5)
            continue

        res = cv2.matchTemplate(target, tmpl, cv2.TM_CCOEFF_NORMED)
        _, maxv, _, maxloc = cv2.minMaxLoc(res)
        log(f"  tap_image score: {maxv:.3f}")
        if maxv >= threshold:
            cx = rx + maxloc[0] + tmpl.shape[1] // 2 + offset_x
            cy = ry + maxloc[1] + tmpl.shape[0] // 2 + offset_y
            log(f"  → タップ: ({cx}, {cy})")
            input_swipe(serial, cx, cy, cx, cy, 100)
            return True
        time.sleep(0.5)
    return False


def _wait_image(serial: str, step: Step, log: LogFn, should_stop: StopFn) -> bool:
    p = step.params
    template_path = p["template"]
    threshold = float(p.get("threshold", 0.85))
    timeout_s = float(p.get("timeout_s", 30))
    region = p.get("region")  # [x, y, w, h] or None

    tmpl = cv2.imread(template_path, cv2.IMREAD_COLOR)
    if tmpl is None:
        log(f"テンプレート読み込み失敗: {template_path}")
        return False

    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if should_stop():
            return False
        try:
            png = screencap(serial)
        except Exception as e:
            log(f"  screencap エラー: {e}")
            time.sleep(0.5)
            continue
        arr = np.frombuffer(png, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            time.sleep(0.3)
            continue

        target = img
        if region and len(region) == 4:
            x, y, w, h = region
            h_img, w_img = img.shape[:2]
            x2, y2 = min(x + w, w_img), min(y + h, h_img)
            x, y = max(0, x), max(0, y)
            target = img[y:y2, x:x2]

        if target.shape[0] < tmpl.shape[0] or target.shape[1] < tmpl.shape[1]:
            log(f"  region がテンプレより小さい: region={target.shape[:2]}, tmpl={tmpl.shape[:2]}")
            return False

        res = cv2.matchTemplate(target, tmpl, cv2.TM_CCOEFF_NORMED)
        _, maxv, _, _ = cv2.minMaxLoc(res)
        log(f"  match score: {maxv:.3f}")
        if maxv >= threshold:
            return True
        time.sleep(0.5)
    return False
