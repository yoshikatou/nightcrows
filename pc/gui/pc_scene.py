"""PC シーン実行エンジン。

PC シーン JSON を読み込み、各ステップを順次実行する。

ステップタイプ:
- snapshot:     シーン編集時のスクショ撮影マーカー（path = "snapshots/..."）。
                ステップ番号・キャンバス切替の文脈情報として JSON に残るが、
                再生時は即スキップする no-op。画像マッチ機能ではない。
- wait_image:   キャプチャ + cv2.matchTemplate でテンプレート一致を待つ。
                template = "templates/..." と threshold / timeout_s / region を持つ。
- tap_image:    テンプレートが一致した位置をクリック
                (region で検索範囲を絞れる、tap_offset_x/y でクリック位置をずらせる)
- tap:          PicoMouse.click()（rx/ry = ウィンドウクライアント相対比率 0.0〜1.0）
- swipe:        PicoMouse.press() + move_to() + release()
- scroll:       swipe にジッター付き
                (rx1_jitter / ry1_jitter / rx2_jitter / ry2_jitter / duration_jitter_ms)
- wait_fixed:   time.sleep()
- call_scene:   別シーンを呼ぶ（最大階層 _MAX_CALL_DEPTH）
- if_image:     画像一致なら then_scene、不一致なら else_scene を呼ぶ
- pick_scene:   scenes リストから random / sequential で 1 シーン選んで呼ぶ
- keyevent:     Win32 keybd_event でキー入力を送信
- group_header: 表示用 no-op
"""
from __future__ import annotations

import ctypes
import json
import os
import random
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
_MAX_CALL_DEPTH = 10

# pick_scene の "sequential" モード用カウンタ（プロセス内で状態保持）
_pick_counters: dict[str, int] = {}

# --------------------------------------- キーコード（keyevent ステップ用）
_KEY_VK_MAP: dict[str, int] = {
    "esc": 0x1B, "escape": 0x1B,
    "enter": 0x0D, "return": 0x0D,
    "tab": 0x09,
    "space": 0x20, "spacebar": 0x20,
    "backspace": 0x08, "bs": 0x08,
    "delete": 0x2E, "del": 0x2E,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def _vk_code(key: str) -> int | None:
    k = key.strip().lower()
    if k in _KEY_VK_MAP:
        return _KEY_VK_MAP[k]
    if len(k) == 1:
        ch = k.upper()
        if ch.isalnum():
            return ord(ch)
    return None


def _send_key(key: str, hold_ms: int) -> bool:
    vk = _vk_code(key)
    if vk is None:
        return False
    user32 = ctypes.windll.user32
    KEYEVENTF_KEYDOWN = 0x0000
    KEYEVENTF_KEYUP   = 0x0002
    user32.keybd_event(vk, 0, KEYEVENTF_KEYDOWN, 0)
    time.sleep(max(0.01, hold_ms / 1000))
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    return True


def _resolve_scene_path(name: str) -> str | None:
    """シーン名 or 相対/絶対パスから JSON のフルパスを解決する。"""
    if not name:
        return None
    # 拡張子補完
    if not name.endswith(".json"):
        name = name + ".json"
    # 既に絶対 or 相対パスなら優先
    if os.path.isabs(name) and os.path.exists(name):
        return name
    if os.path.exists(name):
        return name
    candidate = os.path.join(SCENES_DIR, os.path.basename(name))
    if os.path.exists(candidate):
        return candidate
    return None


def _match_template(img, tmpl_path: str, region) -> tuple[bool, float] | None:
    """region 内でテンプレマッチ。(matched, score) を返す。失敗時 None。"""
    tmpl = cv2.imread(tmpl_path, cv2.IMREAD_COLOR)
    if tmpl is None or img is None:
        return None
    ih, iw = img.shape[:2]
    if region and len(region) == 4:
        rx, ry, rw, rh = region
        x0 = max(0, int(rx * iw))
        y0 = max(0, int(ry * ih))
        x1 = min(iw, int((rx + rw) * iw))
        y1 = min(ih, int((ry + rh) * ih))
        if x1 <= x0 or y1 <= y0:
            return None
        target = img[y0:y1, x0:x1]
    else:
        target = img
    if target.shape[0] < tmpl.shape[0] or target.shape[1] < tmpl.shape[1]:
        return None
    res = cv2.matchTemplate(target, tmpl, cv2.TM_CCOEFF_NORMED)
    _, maxv, _, _ = cv2.minMaxLoc(res)
    return True, float(maxv)


def _do_swipe(mouse, hwnd: int, rx1, ry1, rx2, ry2, duration_ms: int) -> None:
    """共通の swipe 動作（移動 + ボタン押下 + 移動 + 解除）。"""
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
    # フローのエントリ候補として一覧に出すかどうか。
    # False のシーンは他シーンから call_scene 等で呼ばれる「部品シーン」扱いで、
    # フロー編集の対象シーン選択肢から除外される。既定 True（既存シーンを壊さない）。
    flow_target: bool = True


# ----------------------------------------------------------------- JSON 入出力
def load_pc_scene(path: str) -> PcScene:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    steps: list[PcStep] = []
    for s in data.get("steps", []):
        s = dict(s)
        t = s.pop("type")
        # 旧形式マイグレーション:
        # 「snapshot で path が templates/ 配下のもの」は元々の画像出現待ち用途。
        # 新仕様では wait_image タイプに分離するので、ここで自動変換する。
        # 「snapshot で path が snapshots/ 配下のもの」は撮影マーカーなのでそのまま
        # 残すが、画像マッチ用の threshold / timeout_s は意味を持たないため除去する。
        if t == "snapshot":
            raw_path = str(s.get("path", "")).replace("\\", "/")
            if raw_path.startswith("templates/") or "/templates/" in raw_path:
                # wait_image に転換
                t = "wait_image"
                new_params: dict = {"template": s.get("path", "")}
                if "threshold" in s:
                    new_params["threshold"] = s["threshold"]
                if "timeout_s" in s:
                    new_params["timeout_s"] = s["timeout_s"]
                if "region" in s:
                    new_params["region"] = s["region"]
                s = new_params
            else:
                # 撮影マーカー: 画像マッチ用パラメータを捨てる
                s = {"path": s.get("path", "")}
        steps.append(PcStep(type=t, params=s))
    return PcScene(
        name=data.get("name", "untitled"),
        window_title=data.get("window_title", ""),
        steps=steps,
        flow_target=bool(data.get("flow_target", True)),
    )


def save_pc_scene(scene: PcScene, path: str) -> None:
    data = {
        "name": scene.name,
        "window_title": scene.window_title,
        "flow_target": scene.flow_target,
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
    depth: int = 0,
    _call_stack: list[str] | None = None,
) -> bool:
    """シーンを実行する。全ステップ完了なら True、停止要求や失敗なら False。

    呼び出し側で should_stop() が True を返したか確認すれば、
    「ユーザー停止」と「実行失敗」を区別できる。

    depth / _call_stack は call_scene / if_image / pick_scene による
    シーン呼び出しの階層管理用（再帰呼び出し時にインクリメント）。
    """
    if hwnd is None and scene.window_title:
        hwnd = find_hwnd_by_title(scene.window_title)
    if _call_stack is None:
        _call_stack = []

    total = len(scene.steps)
    indent = "  " * (1 + depth)
    log(f"{indent}シーン開始: {scene.name}  ({total} ステップ)")

    def _invoke_subscene(scene_name: str, label: str) -> bool | None:
        """ヘルパー: call_scene / if_image / pick_scene の共通サブ実行。

        戻り値: None=サブ呼び出ししなかった / True=成功 / False=失敗
        """
        if not scene_name:
            return None
        if depth + 1 > _MAX_CALL_DEPTH:
            log(f"{indent}    {label}: 階層上限 ({_MAX_CALL_DEPTH}) 到達 — スキップ")
            return None
        if scene_name in _call_stack:
            log(f"{indent}    {label}: 循環参照 {scene_name} — スキップ")
            return None
        path = _resolve_scene_path(scene_name)
        if not path:
            log(f"{indent}    {label}: シーンが見つかりません: {scene_name}")
            return False
        try:
            sub = load_pc_scene(path)
        except Exception as e:
            log(f"{indent}    {label}: 読込失敗 {e}")
            return False
        return run_pc_scene(
            sub, mouse, hwnd, log, should_stop,
            step_callback=None,
            depth=depth + 1,
            _call_stack=_call_stack + [scene_name],
        )

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
            # 編集時のスクショ撮影マーカー。再生では何もしない no-op。
            # ステップ番号維持 / キャンバス切替の文脈用に JSON に残るが、
            # 画像マッチ機能は wait_image ステップが持つ。
            snap_path = p.get("path", "")
            log(f"  [{i+1}/{total}] snapshot {snap_path}  (スクショ撮影 — 再生時スキップ)")

        elif t == "wait_image":
            tmpl_path = p.get("template", p.get("path", ""))
            timeout_s = float(p.get("timeout_s", 10.0))
            threshold = float(p.get("threshold", 0.85))
            region = p.get("region")   # [rx, ry, rw, rh] (0.0〜1.0) or None
            log(f"  [{i+1}/{total}] wait_image {tmpl_path}  "
                f"threshold={threshold} timeout={timeout_s}s")

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
                    if img is not None:
                        result = _match_template(img, tmpl_path, region)
                        if result is not None:
                            _, score = result
                            if score >= threshold:
                                log(f"    一致 score={score:.3f}")
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
            cw, ch = win32gui.GetClientRect(hwnd)[2:4]
            log(f"    → 絶対座標 ({ax}, {ay})  ボタン={button} hold={hold_ms}ms  "
                f"client={cw}x{ch}")
            # HID 相対移動 + クリック（SetCursorPos を使わない確実版）。
            # SetCursorPos 経路は Nightcrows のチート対策でブロックされ、
            # 「画面外パーク → SetCursorPos」方式でも誤タップが発生する事例があるため、
            # シーン再生では HID 直接 (click_at) を採用する。
            try:
                fx, fy = mouse.click_at(ax, ay, button, hold_ms=hold_ms)
                log(f"    → 実カーソル ({fx},{fy})  誤差({fx-ax:+d},{fy-ay:+d})")
            except Exception as e:
                log(f"    ⚠ click_at 例外: {e}")

        elif t == "tap_image":
            tmpl_path = p.get("template", p.get("path", ""))
            threshold = float(p.get("threshold", 0.85))
            timeout_s = float(p.get("timeout_s", 10.0))
            button = str(p.get("button", "L")).upper()
            hold_ms = int(p.get("duration_ms", 50))
            region = p.get("region")   # [rx, ry, rw, rh] (0.0〜1.0) or None
            off_x = int(p.get("tap_offset_x", 0))
            off_y = int(p.get("tap_offset_y", 0))
            log(f"  [{i+1}/{total}] tap_image {tmpl_path}  threshold={threshold}")

            if not _check_hwnd(hwnd, log) or not _check_mouse(mouse, log):
                continue

            tmpl = cv2.imread(tmpl_path, cv2.IMREAD_COLOR)
            if tmpl is None:
                log(f"    エラー: テンプレート画像が読めません: {tmpl_path}")
                return False

            matched = False
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if should_stop():
                    return False
                if not win32gui.IsWindow(hwnd):
                    time.sleep(0.5)
                    continue
                img = capture_window(hwnd)
                if img is None:
                    time.sleep(0.5)
                    continue
                ih, iw = img.shape[:2]
                if region:
                    rx, ry, rw, rh = region
                    x0 = max(0, int(rx * iw))
                    y0 = max(0, int(ry * ih))
                    x1 = min(iw, int((rx + rw) * iw))
                    y1 = min(ih, int((ry + rh) * ih))
                    target = img[y0:y1, x0:x1]
                else:
                    x0 = y0 = 0
                    target = img
                if (target.shape[0] < tmpl.shape[0]
                        or target.shape[1] < tmpl.shape[1]):
                    time.sleep(0.5)
                    continue
                res = cv2.matchTemplate(target, tmpl, cv2.TM_CCOEFF_NORMED)
                _, maxv, _, maxloc = cv2.minMaxLoc(res)
                if maxv >= threshold:
                    cx = x0 + maxloc[0] + tmpl.shape[1] // 2 + off_x
                    cy = y0 + maxloc[1] + tmpl.shape[0] // 2 + off_y
                    origin = win32gui.ClientToScreen(hwnd, (0, 0))
                    ax = origin[0] + cx
                    ay = origin[1] + cy
                    log(f"    一致 score={maxv:.3f} click ({ax}, {ay})")
                    # tap と同様に HID 直接 (click_at) で確実にクリック。
                    try:
                        fx, fy = mouse.click_at(ax, ay, button, hold_ms=hold_ms)
                        log(f"    → 実カーソル ({fx},{fy})  誤差({fx-ax:+d},{fy-ay:+d})")
                    except Exception as e:
                        log(f"    ⚠ click_at 例外: {e}")
                    matched = True
                    break
                time.sleep(0.5)

            if not matched:
                log(f"    タイムアウト: {tmpl_path}")
                return False

        elif t == "swipe":
            rx1 = float(p.get("rx1", 0.5))
            ry1 = float(p.get("ry1", 0.5))
            rx2 = float(p.get("rx2", 0.5))
            ry2 = float(p.get("ry2", 0.5))
            duration_ms = int(p.get("duration_ms", 500))
            log(f"  [{i+1}/{total}] swipe ({rx1:.3f},{ry1:.3f}) -> ({rx2:.3f},{ry2:.3f})")

            if not _check_hwnd(hwnd, log) or not _check_mouse(mouse, log):
                continue
            _do_swipe(mouse, hwnd, rx1, ry1, rx2, ry2, duration_ms)

        elif t == "scroll":
            rx1 = float(p.get("rx1", 0.5))
            ry1 = float(p.get("ry1", 0.5))
            rx2 = float(p.get("rx2", 0.5))
            ry2 = float(p.get("ry2", 0.5))
            duration_ms = int(p.get("duration_ms", 500))
            rx1 += random.uniform(-float(p.get("rx1_jitter", 0.01)), float(p.get("rx1_jitter", 0.01)))
            ry1 += random.uniform(-float(p.get("ry1_jitter", 0.01)), float(p.get("ry1_jitter", 0.01)))
            rx2 += random.uniform(-float(p.get("rx2_jitter", 0.01)), float(p.get("rx2_jitter", 0.01)))
            ry2 += random.uniform(-float(p.get("ry2_jitter", 0.01)), float(p.get("ry2_jitter", 0.01)))
            dur_j = int(p.get("duration_jitter_ms", 100))
            if dur_j > 0:
                duration_ms = max(50, duration_ms + random.randint(-dur_j, dur_j))
            log(
                f"  [{i+1}/{total}] scroll ({rx1:.3f},{ry1:.3f}) -> ({rx2:.3f},{ry2:.3f})  "
                f"({duration_ms}ms ジッター付き)"
            )
            if not _check_hwnd(hwnd, log) or not _check_mouse(mouse, log):
                continue
            _do_swipe(mouse, hwnd, rx1, ry1, rx2, ry2, duration_ms)

        elif t == "keyevent":
            key = str(p.get("key", "")).strip()
            hold_ms = int(p.get("duration_ms", 30))
            log(f"  [{i+1}/{total}] keyevent {key!r}")
            if not key:
                log("    key 未指定 — スキップ")
                continue
            if not _send_key(key, hold_ms):
                log(f"    未知のキー — スキップ")

        elif t == "group_header":
            label = str(p.get("label", ""))
            log(f"  [{i+1}/{total}] ── {label} ──")

        elif t == "call_scene":
            scene_name = str(p.get("scene", ""))
            log(f"  [{i+1}/{total}] call_scene → {scene_name}")
            r = _invoke_subscene(scene_name, "call_scene")
            if r is False:
                return False

        elif t == "if_image":
            tmpl_path = str(p.get("template", p.get("path", "")))
            threshold = float(p.get("threshold", 0.85))
            region    = p.get("region")
            then_name = str(p.get("then_scene", ""))
            else_name = str(p.get("else_scene", ""))
            log(f"  [{i+1}/{total}] if_image {tmpl_path}")
            if not _check_hwnd(hwnd, log):
                continue
            img = capture_window(hwnd)
            if img is None:
                log("    キャプチャ失敗 → else 側で続行")
                matched = False
            else:
                m = _match_template(img, tmpl_path, region)
                if m is None:
                    log("    判定不能（テンプレ/領域問題）→ else 側で続行")
                    matched = False
                else:
                    _, score = m
                    matched = score >= threshold
                    log(f"    score={score:.3f}  threshold={threshold:.2f}  → {'then' if matched else 'else'}")
            target_name = then_name if matched else else_name
            r = _invoke_subscene(target_name, "if_image")
            if r is False:
                return False

        elif t == "pick_scene":
            scenes = list(p.get("scenes", []) or [])
            mode = str(p.get("mode", "random"))
            step_id = str(p.get("step_id", "")) or f"{scene.name}#{i}"
            if not scenes:
                log(f"  [{i+1}/{total}] pick_scene: シーン未指定 — スキップ")
                continue
            if mode == "sequential":
                idx = _pick_counters.get(step_id, 0) % len(scenes)
                _pick_counters[step_id] = (idx + 1) % len(scenes)
                chosen = scenes[idx]
            else:
                chosen = random.choice(scenes)
            log(f"  [{i+1}/{total}] pick_scene[{mode}] → {chosen}")
            r = _invoke_subscene(chosen, "pick_scene")
            if r is False:
                return False

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
