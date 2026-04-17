"""ADB / scrcpy 操作のラッパ。"""
from __future__ import annotations

import os
import re
import subprocess

ADB = r"C:\scrcpy\adb.exe"
SCRCPY = r"C:\scrcpy\scrcpy.exe"

DEFAULT_SERIAL = os.environ.get("ADB_SERIAL", "192.168.255.57:34497")


def screencap(serial: str, timeout: float = 10.0) -> bytes:
    """現在の画面の PNG バイト列を返す。"""
    r = subprocess.run(
        [ADB, "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"screencap failed: {r.stderr.decode(errors='replace')}")
    return r.stdout


def input_swipe(serial: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
    duration_ms = max(1, int(duration_ms))
    subprocess.run(
        [ADB, "-s", serial, "shell", "input", "swipe",
         str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(duration_ms)],
        capture_output=True, timeout=duration_ms // 1000 + 5,
    )


def launch_scrcpy(serial: str) -> subprocess.Popen:
    """scrcpy プレビューを別ウィンドウで起動する。"""
    return subprocess.Popen(
        [SCRCPY, "-s", serial,
         "--window-title", f"nightcrows preview ({serial})"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def get_rotation_and_size(serial: str) -> tuple[int, int, int]:
    """(rotation, phys_w, phys_h) を返す。tap_record.py と同じロジック。"""
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
