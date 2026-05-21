"""経験値計測コア（モバイル版 ExpMeterWidget から移植・PC向け改修）。

mobile 版との違い:
- キャプチャは ADB ではなく Win32 PrintWindow（capture_window）
- 領域は絶対 px ではなくウィンドウクライアント領域に対する比率 [rx, ry, rw, rh]
- 領域は毎回キャプチャ画像のサイズから絶対 px に変換するので、
  ゲームウィンドウのサイズ変更に追従する
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime as _dt
from datetime import timedelta

import cv2
import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from .capture import capture_window
from .ocr import ocr_digits_best
from .window_picker import find_hwnd_by_title

EXP_METER_PATH = "exp_meter.json"
DEFAULT_INTERVAL_SEC = 30            # 計測間隔の初期値（秒）
CURRENT_WINDOW = 3                   # 現在速度: 直近Nサンプル
OCR_TRIES = 3
OCR_INTERVAL_S = 1.5
LOG_DIR = "logs"
DEFAULT_LOG_RETAIN_DAYS = 30         # この日数より古いログを起動時に削除（settings.json で上書き可）


def apply_digit_hint(ocr_raw: str, hint: int) -> float | None:
    """OCR文字列に桁数ヒントを適用して float を返す。"""
    pure = "".join(c for c in ocr_raw if c.isdigit())
    if len(pure) <= hint:
        return None
    fixed = pure[:hint] + "." + pure[hint:]
    val = float(fixed)
    return val if 0.0 <= val <= 100.0 else None


def calc_speed(samples: list[tuple[_dt, float]]) -> float | None:
    """サンプル（2点以上）から %/h を返す。"""
    if len(samples) < 2:
        return None
    elapsed_h = (samples[-1][0] - samples[0][0]).total_seconds() / 3600
    if elapsed_h <= 0:
        return None
    return (samples[-1][1] - samples[0][1]) / elapsed_h


class ExpMeter(QObject):
    """経験値計測のコアロジック（UI非依存）。

    シグナル:
        updated()            – 状態が変化した（UI再描画用）
        status_changed(str)  – 一行ステータス
        error(str)           – エラー
    """

    updated         = Signal()
    status_changed  = Signal(str)
    error           = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.window_title:  str  = ""
        self.region_rel:    list[float] = []
        self.digit_hint:    int  = 1
        self.interval_sec:  int  = DEFAULT_INTERVAL_SEC
        self.samples:       list[tuple[_dt, float]] = []
        self.prev_raw:      float | None = None
        self.accumulated:   float = 0.0
        self.start_time:    _dt | None   = None
        self.running:       bool = False
        self.last_ocr_detail: str = ""

        self._sample_ready  = _SignalRelay()
        self._sample_failed = _SignalRelay()
        self._sample_ready.s.connect(self._on_sample_ready)
        self._sample_failed.s.connect(self._on_sample_failed)

        self._timer = QTimer(self)
        self._timer.setInterval(self.interval_sec * 1000)
        self._timer.timeout.connect(self._do_sample)

        self._clock = QTimer(self)
        self._clock.setInterval(60_000)
        self._clock.timeout.connect(self.updated.emit)
        self._clock.start()

        self.load()

    # ---------------------------------------------------------------- 永続化
    def load(self) -> None:
        import json
        if not os.path.exists(EXP_METER_PATH):
            return
        try:
            with open(EXP_METER_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.window_title = d.get("window_title", "")
            self.region_rel   = list(d.get("region_rel", []))
            self.digit_hint   = int(d.get("digit_hint", 1))
            # 旧 interval_min（分）からの移行も拾う
            if "interval_sec" in d:
                self.interval_sec = max(5, int(d["interval_sec"]))
            elif "interval_min" in d:
                self.interval_sec = max(5, int(d["interval_min"]) * 60)
            else:
                self.interval_sec = DEFAULT_INTERVAL_SEC
            self._timer.setInterval(self.interval_sec * 1000)
            self.samples = [
                (_dt.fromisoformat(ts), float(acc))
                for ts, acc in d.get("samples", [])
            ]
            self.accumulated = float(d.get("accumulated", 0.0))
            self.prev_raw    = (float(d["prev_raw"]) if d.get("prev_raw") is not None
                                else None)
            st = d.get("start_time")
            self.start_time  = _dt.fromisoformat(st) if st else None
        except Exception:
            pass

    def save(self) -> None:
        import json
        try:
            with open(EXP_METER_PATH, "w", encoding="utf-8") as f:
                json.dump({
                    "window_title": self.window_title,
                    "region_rel":   self.region_rel,
                    "digit_hint":   self.digit_hint,
                    "interval_sec": self.interval_sec,
                    "samples":      [[ts.isoformat(), acc] for ts, acc in self.samples],
                    "accumulated":  self.accumulated,
                    "prev_raw":     self.prev_raw,
                    "start_time":   self.start_time.isoformat() if self.start_time else None,
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------------------------------------------------------------- 計測制御
    def start(self) -> bool:
        if not self.window_title or not self.region_rel:
            self.error.emit("ウィンドウと領域を設定してください")
            return False
        if not find_hwnd_by_title(self.window_title):
            self.error.emit(f"ウィンドウが見つかりません: {self.window_title}")
            return False
        self.running = True
        if self.start_time is None:
            self.start_time = _dt.now()
        self._timer.start()
        self.status_changed.emit("計測中…")
        self._do_sample()
        self.updated.emit()
        return True

    def stop(self) -> None:
        self.running = False
        self._timer.stop()
        self.status_changed.emit("停止中")
        self.updated.emit()

    def set_interval(self, seconds: int) -> None:
        """計測間隔（秒）を変更。実行中なら次回タイマーから反映。"""
        seconds = max(5, int(seconds))
        self.interval_sec = seconds
        self._timer.setInterval(seconds * 1000)
        self.save()

    def reset(self) -> None:
        was_running = self.running
        self.stop()
        self.samples.clear()
        self.prev_raw    = None
        self.accumulated = 0.0
        self.start_time  = None
        self.save()
        self.updated.emit()
        if was_running:
            self.start()

    # ---------------------------------------------------------------- サンプリング
    def _log(self, msg: str) -> None:
        now = _dt.now()
        line = f"[{now.strftime('%H:%M:%S')}] 📊 経験値計測: {msg}\n"
        os.makedirs(LOG_DIR, exist_ok=True)
        try:
            with open(
                os.path.join(LOG_DIR, f"{now.strftime('%Y-%m-%d')}.log"),
                "a", encoding="utf-8",
            ) as f:
                f.write(line)
        except Exception:
            pass

    @staticmethod
    def purge_old_logs(retain_days: int = DEFAULT_LOG_RETAIN_DAYS) -> int:
        """retain_days より古い .log ファイルを削除し、削除数を返す。"""
        if not os.path.isdir(LOG_DIR):
            return 0
        removed = 0
        try:
            cutoff = _dt.now() - timedelta(days=max(1, retain_days))
            for fname in os.listdir(LOG_DIR):
                if not fname.endswith(".log"):
                    continue
                stem = fname[:-4]
                try:
                    file_date = _dt.strptime(stem, "%Y-%m-%d")
                except ValueError:
                    continue
                if file_date < cutoff:
                    try:
                        os.remove(os.path.join(LOG_DIR, fname))
                        removed += 1
                    except Exception:
                        pass
        except Exception:
            pass
        return removed

    def _do_sample(self) -> None:
        if not self.window_title or not self.region_rel:
            return
        self.status_changed.emit("取得中…")
        threading.Thread(
            target=self._sample_worker,
            args=(self.window_title, list(self.region_rel), self.digit_hint),
            daemon=True,
        ).start()

    def _sample_worker(
        self, title: str, region_rel: list[float], digit_hint: int,
    ) -> None:
        try:
            config = "--psm 7 --oem 3 -c tessedit_char_whitelist=0123456789."
            readings: list[float] = []
            raw_log:  list[str]   = []

            for i in range(OCR_TRIES):
                if i > 0:
                    time.sleep(OCR_INTERVAL_S)
                try:
                    hwnd = find_hwnd_by_title(title)
                    if not hwnd:
                        raw_log.append(f"#{i+1}:ウィンドウ消失")
                        continue
                    img = capture_window(hwnd)
                    if img is None:
                        raw_log.append(f"#{i+1}:キャプチャ失敗")
                        continue
                    ih, iw = img.shape[:2]
                    rx, ry, rw, rh = region_rel
                    x = int(rx * iw); y = int(ry * ih)
                    w = int(rw * iw); h = int(rh * ih)
                    crop = img[max(0, y):min(y + h, ih), max(0, x):min(x + w, iw)]
                    if crop.size == 0:
                        raw_log.append(f"#{i+1}:領域外")
                        continue
                    digits, _ = ocr_digits_best(crop, config)
                    if not digits:
                        raw_log.append(f"#{i+1}:OCR失敗")
                        continue
                    val = apply_digit_hint(digits, digit_hint)
                    if val is None:
                        raw_log.append(f"#{i+1}:{digits!r}→ヒント適用失敗")
                        continue
                    readings.append(val)
                    raw_log.append(f"#{i+1}:{digits!r}→{val:.4f}%")
                except Exception as e:
                    raw_log.append(f"#{i+1}:エラー({e})")

            summary = "  ".join(raw_log)
            if not readings:
                self._sample_failed.s.emit(f"全試行失敗 [{summary}]")
                return
            readings.sort()
            chosen = readings[len(readings) // 2]
            self.last_ocr_detail = f"採用={chosen:.4f}%  hint={digit_hint}桁  試行=[{summary}]"
            self._sample_ready.s.emit(chosen)
        except Exception as e:
            self._sample_failed.s.emit(str(e))

    def _on_sample_ready(self, raw: float) -> None:
        now = _dt.now()
        detail = self.last_ocr_detail
        if self.prev_raw is None:
            self.prev_raw    = raw
            self.accumulated = 0.0
            self.samples.append((now, 0.0))
            self._log(f"初回取得  累積=0.0000%  {detail}")
        else:
            delta = raw - self.prev_raw
            if delta < -30:
                valid_delta = (100.0 - self.prev_raw) + raw
                self.accumulated += valid_delta
                self.prev_raw = raw
                self.samples.append((now, self.accumulated))
                self._log(
                    f"LvUP検知  delta={delta:+.4f}%"
                    f"  加算={valid_delta:.4f}%  累積={self.accumulated:.4f}%  {detail}"
                )
            elif delta < 0:
                self._log(
                    f"誤読スキップ  delta={delta:+.4f}%"
                    f"  prev={self.prev_raw:.4f}%  {detail}"
                )
                self.status_changed.emit(
                    f"⚠ 誤読スキップ ({raw:.2f}%)  最終: {now.strftime('%H:%M')}")
                return
            else:
                self.accumulated += delta
                self.prev_raw = raw
                self.samples.append((now, self.accumulated))
                self._log(
                    f"取得  delta={delta:+.4f}%"
                    f"  累積={self.accumulated:.4f}%  {detail}"
                )
        self.status_changed.emit(f"最終取得: {now.strftime('%H:%M')}")
        self.save()
        self.updated.emit()

    def _on_sample_failed(self, msg: str) -> None:
        self._log(f"エラー  {msg}")
        self.status_changed.emit(f"⚠ {msg}")

    # ---------------------------------------------------------------- 表示計算
    def current_speed(self) -> float | None:
        if len(self.samples) < CURRENT_WINDOW:
            return None
        return calc_speed(self.samples[-CURRENT_WINDOW:])

    def avg_speed(self) -> float | None:
        return calc_speed(self.samples)

    def eta_to_levelup(self) -> tuple[float | None, float | None]:
        """(現在速度ベース分, 平均速度ベース分) — 不明は None。"""
        if self.prev_raw is None:
            return None, None
        remaining = 100.0 - self.prev_raw
        cur = self.current_speed()
        avg = self.avg_speed()
        def _eta(spd: float | None) -> float | None:
            return remaining / spd * 60 if spd and spd > 0 else None
        return _eta(cur), _eta(avg)

    def elapsed_str(self) -> str:
        if not self.start_time:
            return "—"
        s = int((_dt.now() - self.start_time).total_seconds())
        h, rem = divmod(s, 3600)
        return f"{h}h {rem // 60:02d}m" if h > 0 else f"{rem // 60}m"


class _SignalRelay(QObject):
    """ワーカースレッドからメインスレッドへ float/str を投げるための中継。"""
    s = Signal(object)
