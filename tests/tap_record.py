"""
人間のタップを記録する

使用方法:
    python tests/tap_record.py recordings/session1.json
    python tests/tap_record.py recordings/session1.json --serial 192.168.0.119:35103

環境変数 ADB_SERIAL でデフォルトシリアルを指定可能。
Ctrl+C で記録を終了し JSON に保存する（連打しても安全）。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time

ADB = r"C:\scrcpy\adb.exe"
TOUCH_DEVICE = "/dev/input/event3"
SCALE = 10  # getevent は物理解像度の10倍で出力される

DEFAULT_SERIAL = os.environ.get("ADB_SERIAL", "192.168.255.57:34497")


def get_rotation_and_size(serial: str) -> tuple[int, int, int]:
    """現在の画面回転と物理サイズを返す。

    Returns:
        (rotation, phys_w, phys_h)
        rotation: 0=縦, 1=横(90), 2=逆縦(180), 3=横(270)
        phys_w, phys_h: 縦向き基準の物理解像度
    """
    phys_w, phys_h = 1220, 2712
    r = subprocess.run(
        [ADB, "-s", serial, "shell", "wm", "size"],
        capture_output=True, text=True, timeout=5,
    )
    for line in r.stdout.splitlines():
        if "Physical size" in line or "Override size" in line:
            part = line.split(":")[-1].strip()
            try:
                phys_w, phys_h = map(int, part.split("x"))
            except ValueError:
                pass
            break

    rotation = 0
    r2 = subprocess.run(
        [ADB, "-s", serial, "shell", "dumpsys", "input"],
        capture_output=True, text=True, timeout=5,
    )
    for line in r2.stdout.splitlines():
        m = re.search(r"orientation=(\d+)", line)
        if m and "Viewport" in line:
            rotation = int(m.group(1))
            break
    return rotation, phys_w, phys_h


def phys_to_logical(x_p: int, y_p: int, rotation: int,
                    phys_w: int, phys_h: int) -> tuple[int, int]:
    """getevent の物理座標 → input swipe が期待する論理座標へ変換。"""
    if rotation == 1:  # ROTATION_90
        return y_p, phys_w - x_p
    if rotation == 2:  # ROTATION_180
        return phys_w - x_p, phys_h - y_p
    if rotation == 3:  # ROTATION_270
        return phys_h - y_p, x_p
    return x_p, y_p  # ROTATION_0


def record(serial: str, output_path: str) -> None:
    rotation, phys_w, phys_h = get_rotation_and_size(serial)
    if rotation in (1, 3):
        log_w, log_h = phys_h, phys_w
    else:
        log_w, log_h = phys_w, phys_h

    print(f"記録開始: serial={serial}")
    print(f"出力先 : {output_path}")
    print(f"画面   : rotation={rotation}, physical={phys_w}x{phys_h}, logical={log_w}x{log_h}")
    print("タップしてください。Ctrl+C で終了し保存します。")
    print("-" * 48)

    cmd = [ADB, "-s", serial, "shell", "getevent", "-l", TOUCH_DEVICE]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    # getevent 出力は別スレッドでキューに流す
    # → メインスレッドは poll で stop_event を定期的に確認できる
    q: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    def reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        finally:
            q.put(None)  # EOF マーカー

    threading.Thread(target=reader, daemon=True).start()

    # Ctrl+C は例外を投げず、stop_event を立てて subprocess を終了させる
    # → メインループ内で安全に break できる
    # 連打されても on_sigint は idempotent（二重 terminate は例外握り潰し）
    def on_sigint(signum, frame) -> None:
        stop_event.set()
        try:
            proc.terminate()
        except Exception:
            pass

    signal.signal(signal.SIGINT, on_sigint)

    events: list[dict] = []
    t_base: float | None = None

    x1 = y1 = x2 = y2 = None
    t_start: float | None = None
    in_touch = False

    while not stop_event.is_set():
        try:
            line = q.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:
            break

        txt = line.decode(errors="replace").strip()
        now = time.monotonic()

        if "ABS_MT_TRACKING_ID" in txt:
            m = re.search(r"([0-9a-f]+)$", txt)
            if not m:
                continue
            val = int(m.group(1), 16)
            if val == 0xFFFFFFFF:
                # 指が離れた
                if in_touch and x1 is not None and x2 is not None and t_start is not None:
                    duration_ms = max(1, int((now - t_start) * 1000))
                    if t_base is None:
                        t_base = t_start
                    # 物理 → 論理座標へ変換して保存する
                    lx1, ly1 = phys_to_logical(x1, y1, rotation, phys_w, phys_h)
                    lx2, ly2 = phys_to_logical(x2, y2, rotation, phys_w, phys_h)
                    ev = {
                        "t": round(t_start - t_base, 3),
                        "x1": lx1, "y1": ly1,
                        "x2": lx2, "y2": ly2,
                        "duration_ms": duration_ms,
                    }
                    events.append(ev)
                    kind = "TAP  " if (abs(lx2 - lx1) < 10 and abs(ly2 - ly1) < 10) else "SWIPE"
                    print(f"[{ev['t']:7.2f}s] {kind} ({lx1:4d},{ly1:4d})->({lx2:4d},{ly2:4d}) {duration_ms}ms")
                x1 = y1 = x2 = y2 = None
                t_start = None
                in_touch = False
            else:
                # 指が触れた
                in_touch = True
                t_start = now
                x1 = y1 = x2 = y2 = None

        elif in_touch and "ABS_MT_POSITION_X" in txt:
            m = re.search(r"([0-9a-f]+)$", txt)
            if m:
                v = int(m.group(1), 16) // SCALE
                if x1 is None:
                    x1 = v
                x2 = v

        elif in_touch and "ABS_MT_POSITION_Y" in txt:
            m = re.search(r"([0-9a-f]+)$", txt)
            if m:
                v = int(m.group(1), 16) // SCALE
                if y1 is None:
                    y1 = v
                y2 = v

    print("\n記録停止")
    try:
        proc.terminate()
    except Exception:
        pass

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "serial": serial,
            "rotation": rotation,
            "phys_size": [phys_w, phys_h],
            "logical_size": [log_w, log_h],
            "events": events,
        }, f, indent=2, ensure_ascii=False)
    print(f"保存完了: {len(events)} 件 -> {output_path}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("output", help="出力 JSON パス (例: recordings/session1.json)")
    p.add_argument("--serial", default=DEFAULT_SERIAL,
                   help=f"ADB シリアル (default: {DEFAULT_SERIAL})")
    args = p.parse_args()
    record(args.serial, args.output)


if __name__ == "__main__":
    main()
