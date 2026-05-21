"""実機タップをリアルタイムで記録し、シグナルで通知するレコーダ。

getevent 出力を論理座標に変換しながら、タップが完了するたびに
tap_detected(x, y, duration_ms, gap_s) を発火する。
"""
from __future__ import annotations

import re
import subprocess
import threading
import time

from PySide6.QtCore import QObject, Signal

from .adb import ADB, get_rotation_and_size

TOUCH_DEVICE = "/dev/input/event3"
SCALE = 10


class TapRecorder(QObject):
    tap_detected = Signal(int, int, int, float)   # logical x, y, duration_ms, gap_since_prev_s
    error = Signal(str)
    stopped = Signal()

    def __init__(self, serial: str) -> None:
        super().__init__()
        self._serial = serial
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        p = self._proc
        if p:
            try:
                p.terminate()
            except Exception:
                pass

    # ----------------------------------------------------------------- impl
    def _run(self) -> None:
        try:
            rotation, phys_w, phys_h = get_rotation_and_size(self._serial)
        except Exception as e:
            self.error.emit(f"回転取得失敗: {e}")
            self.stopped.emit()
            return

        def to_logical(x_p: int, y_p: int) -> tuple[int, int]:
            if rotation == 1:
                return y_p, phys_w - x_p
            if rotation == 2:
                return phys_w - x_p, phys_h - y_p
            if rotation == 3:
                return phys_h - y_p, x_p
            return x_p, y_p

        cmd = [ADB, "-s", self._serial, "shell", "getevent", "-l", TOUCH_DEVICE]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self.error.emit(f"getevent 起動失敗: {e}")
            self.stopped.emit()
            return

        x1 = y1 = x2 = y2 = None
        t_start: float | None = None
        in_touch = False
        last_tap_end: float | None = None

        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                txt = line.decode(errors="replace").strip()
                now = time.monotonic()

                if "ABS_MT_TRACKING_ID" in txt:
                    m = re.search(r"([0-9a-f]+)$", txt)
                    if not m:
                        continue
                    val = int(m.group(1), 16)
                    if val == 0xFFFFFFFF:
                        if (in_touch and x1 is not None and y1 is not None
                                and t_start is not None):
                            duration_ms = max(1, int((now - t_start) * 1000))
                            lx, ly = to_logical(x1, y1)
                            gap = 0.0 if last_tap_end is None else max(0.0, t_start - last_tap_end)
                            self.tap_detected.emit(lx, ly, duration_ms, gap)
                            last_tap_end = now
                        x1 = y1 = x2 = y2 = None
                        t_start = None
                        in_touch = False
                    else:
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
        except Exception as e:
            self.error.emit(f"読み取りエラー: {e}")
        finally:
            try:
                if self._proc:
                    self._proc.terminate()
            except Exception:
                pass
            self.stopped.emit()
