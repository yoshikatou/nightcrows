"""ウィンドウ録画ワーカー。

対象ウィンドウを `capture_window` で連続キャプチャし、`cv2.VideoWriter`
で mp4 に書き出すバックグラウンド録画器。

- 既定 fps=2。寝てる間 8h でも数百 MB 級に収まる
- 1 時間ごとに新ファイルに分割（巨大化・破損時の被害を限定）
- 出力先: recordings/rec_YYYYMMDD_HHMMSS.mp4
- ウィンドウが一時的に取れない（フォアグラウンド奪われ・最小化等）場合は
  直前フレームを書き続ける（録画はセッションを切らない方針）
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime

import cv2
from PySide6.QtCore import QObject, Signal

from .capture import capture_window

RECORDINGS_DIR    = "recordings"
SPLIT_INTERVAL_S  = 3600           # 1 時間ごとに新ファイル
FOURCC            = cv2.VideoWriter_fourcc(*"mp4v")


class WindowRecorder(QObject):
    """対象ウィンドウを mp4 に連続録画するワーカー。

    Qt 側からは start/stop と各シグナルのみで使う。
    内部処理はデーモンスレッドで実行される。
    """

    started        = Signal(str)         # 出力先パス（最初の分割ファイル）
    stopped        = Signal()
    file_rotated   = Signal(str)         # 分割で新ファイルを開いた
    stats_updated  = Signal(int, float)  # (累計フレーム数, 経過秒数)
    error_occurred = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._thread: threading.Thread | None = None
        self._stop_flag = False
        self._current_path: str | None = None
        self._frame_count = 0
        self._start_time = 0.0

    @property
    def is_recording(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def current_path(self) -> str | None:
        return self._current_path

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def elapsed_s(self) -> float:
        if not self.is_recording:
            return 0.0
        return time.time() - self._start_time

    def start(self, hwnd: int, fps: float = 2.0) -> None:
        if self.is_recording:
            return
        self._stop_flag = False
        self._frame_count = 0
        self._start_time = time.time()
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        self._thread = threading.Thread(
            target=self._run, args=(hwnd, fps), daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_flag = True

    # ---- 内部
    def _make_writer(
        self, fps: float, w: int, h: int,
    ) -> tuple[cv2.VideoWriter, str]:
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(RECORDINGS_DIR, f"rec_{ts}.mp4").replace("\\", "/")
        writer = cv2.VideoWriter(path, FOURCC, fps, (w, h))
        if not writer.isOpened():
            raise RuntimeError(f"VideoWriter を開けません: {path}")
        return writer, path

    def _run(self, hwnd: int, fps: float) -> None:
        writer: cv2.VideoWriter | None = None
        try:
            interval = 1.0 / max(fps, 0.1)

            # 最初のフレームで解像度を確定
            first = capture_window(hwnd)
            if first is None:
                self.error_occurred.emit("最初のフレーム取得に失敗")
                return
            h, w = first.shape[:2]

            writer, path = self._make_writer(fps, w, h)
            self._current_path = path
            self.started.emit(path)
            writer.write(first)
            self._frame_count = 1
            last_frame = first
            split_anchor = time.time()
            next_tick = time.time() + interval

            while not self._stop_flag:
                now = time.time()
                remain = next_tick - now
                if remain > 0:
                    # 細かく刻んで stop 反応を保つ
                    time.sleep(min(remain, 0.2))
                    continue
                # スケジュール: 取り遅れた場合は追いつかせる
                next_tick += interval
                if next_tick < now - interval:
                    next_tick = now + interval

                frame = capture_window(hwnd)
                if frame is None or not frame.any():
                    # ウィンドウが落ちた/隠れた一時状態: 直前フレームを継続して書く
                    # （タイムラインの長さを失わないため）
                    frame = last_frame
                elif frame.shape[:2] != (h, w):
                    # 録画中の解像度変動はサイズ合わせて吸収
                    frame = cv2.resize(frame, (w, h))
                writer.write(frame)
                last_frame = frame
                self._frame_count += 1

                # 統計: 5 フレームに 1 回（fps=2 なら 2.5 秒間隔）
                if self._frame_count % 5 == 0:
                    self.stats_updated.emit(
                        self._frame_count, time.time() - self._start_time,
                    )

                # 1 時間ごとに新ファイルへロール
                if time.time() - split_anchor >= SPLIT_INTERVAL_S:
                    writer.release()
                    writer, path = self._make_writer(fps, w, h)
                    self._current_path = path
                    split_anchor = time.time()
                    self.file_rotated.emit(path)

        except Exception as e:
            self.error_occurred.emit(f"録画エラー: {e}")
        finally:
            if writer is not None:
                try:
                    writer.release()
                except Exception:
                    pass
            self.stats_updated.emit(
                self._frame_count,
                time.time() - self._start_time if self._start_time else 0.0,
            )
            self.stopped.emit()
