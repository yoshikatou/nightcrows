"""録画モード — 一定間隔で画面スナップショットを保存する。"""
from __future__ import annotations

import os
import shutil
import threading
import time
from datetime import datetime, time as dtime, timedelta
from typing import Callable

import cv2
import numpy as np

from .adb import screencap

LogFn = Callable[[str], None]


def human_bytes(n: int) -> str:
    f = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} TB"


def folder_size_bytes(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def cleanup_old_folders(out_dir: str, keep_days: int, log_fn: LogFn = print) -> int:
    """`out_dir/YYYY-MM-DD/` 形式のフォルダで keep_days 日より古いものを削除。"""
    if not os.path.isdir(out_dir):
        return 0
    today = datetime.now().date()
    removed = 0
    for name in os.listdir(out_dir):
        path = os.path.join(out_dir, name)
        if not os.path.isdir(path):
            continue
        try:
            d = datetime.strptime(name, "%Y-%m-%d").date()
        except ValueError:
            continue
        age = (today - d).days
        if age > keep_days:
            try:
                shutil.rmtree(path)
                removed += 1
                log_fn(f"録画: 古いフォルダ削除 ({age}日前) {name}")
            except Exception as e:
                log_fn(f"録画: フォルダ削除失敗 {name}: {e}")
    return removed


class ScreenRecorder:
    """別スレッドで定期スクショを撮る録画器。"""

    def __init__(
        self,
        serial: str,
        out_dir: str,
        interval_s: float,
        jpeg_quality: int = 85,
        auto_stop_at: dtime | None = None,
        log_fn: LogFn = print,
    ) -> None:
        self._serial = serial
        self._out_dir = out_dir
        self._interval_s = max(1.0, float(interval_s))
        self._quality = max(50, min(95, int(jpeg_quality)))
        self._auto_stop_at = auto_stop_at
        self._log = log_fn

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._count = 0
        self._total_bytes = 0
        self._last_path = ""

    def start(self) -> None:
        if self.is_running():
            return
        os.makedirs(self._out_dir, exist_ok=True)
        self._stop.clear()
        self._count = 0
        self._total_bytes = 0
        self._last_path = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def count(self) -> int:
        return self._count

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def last_path(self) -> str:
        return self._last_path

    def _compute_stop_dt(self, started_at: datetime) -> datetime | None:
        if self._auto_stop_at is None:
            return None
        target = datetime.combine(started_at.date(), self._auto_stop_at)
        if target <= started_at:
            target = datetime.combine(started_at.date() + timedelta(days=1), self._auto_stop_at)
        return target

    def _run(self) -> None:
        started_at = datetime.now()
        stop_dt = self._compute_stop_dt(started_at)
        if stop_dt:
            self._log(f"録画: 自動停止予定 {stop_dt:%Y-%m-%d %H:%M}")

        next_capture = time.monotonic()
        while not self._stop.is_set():
            now = datetime.now()
            if stop_dt is not None and now >= stop_dt:
                self._log(f"録画: 自動停止時刻 {stop_dt:%H:%M} に到達")
                break

            if time.monotonic() >= next_capture:
                self._capture_one(now)
                next_capture = time.monotonic() + self._interval_s

            self._stop.wait(0.5)

        self._log(f"録画停止: {self._count} 枚 / {human_bytes(self._total_bytes)}")

    def _capture_one(self, now: datetime) -> None:
        try:
            png = screencap(self._serial)
            arr = np.frombuffer(png, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                self._log("録画: imdecode 失敗（画像が壊れている可能性）")
                return
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self._quality])
            if not ok:
                self._log("録画: imencode 失敗")
                return

            day_dir = os.path.join(self._out_dir, now.strftime("%Y-%m-%d"))
            os.makedirs(day_dir, exist_ok=True)
            fname = now.strftime("snap_%H%M%S.jpg")
            fpath = os.path.join(day_dir, fname)
            buf.tofile(fpath)

            size = os.path.getsize(fpath)
            self._count += 1
            self._total_bytes += size
            self._last_path = fpath
        except Exception as e:
            self._log(f"録画エラー: {e}")
