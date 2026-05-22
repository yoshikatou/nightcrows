"""経験値計測コア（モバイル版 ExpMeterWidget から移植・PC向け改修）。

mobile 版との違い:
- キャプチャは ADB ではなく Win32 PrintWindow（capture_window）
- 領域は絶対 px ではなくウィンドウクライアント領域に対する比率 [rx, ry, rw, rh]
- 領域は毎回キャプチャ画像のサイズから絶対 px に変換するので、
  ゲームウィンドウのサイズ変更に追従する
"""
from __future__ import annotations

import os
import statistics
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
CURRENT_WINDOW = 2                   # 現在速度の算出に必要な最低サンプル数
CURRENT_DELTA_SAMPLES = 10           # 現在速度: 直近 N 個の隣接Δから中央値で算出
MEDIAN_WINDOW = 5                    # 生値の平滑化: 直近Nサンプルの中央値を真値とみなす
LVUP_DROP_THRESHOLD = 30.0           # 平滑化値がこの%以上下落したら LvUP とみなす
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
        # 生値の系列（時刻, OCR読み取り値）。平滑化・累積・時速は毎回ここから導出する
        self._raw_samples:  list[tuple[_dt, float]] = []
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
            # 新形式: raw_samples（生値）。旧形式（accumulated/samples/prev_raw）は
            # 生値を復元できないため破棄して新規スタート。
            raw = d.get("raw_samples")
            if raw is not None:
                self._raw_samples = [
                    (_dt.fromisoformat(ts), float(v)) for ts, v in raw
                ]
            else:
                self._raw_samples = []
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
                    "raw_samples":  [[ts.isoformat(), v] for ts, v in self._raw_samples],
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
        self._raw_samples.clear()
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
        """OCR成功時: 生値を _raw_samples に追加する。平滑化と累積は派生で計算。"""
        now = _dt.now()
        detail = self.last_ocr_detail

        # 追加前の派生値を退避してログ用に差分を出す
        cs_before = self._cumulative_series()
        prev_filtered = cs_before[-1][1] if cs_before else None
        acc_before = self.accumulated  # this internally derives — cheap enough

        self._raw_samples.append((now, raw))

        # 追加後の最新平滑化値・累積
        cs_after = self._cumulative_series()
        latest_filtered = self._filtered_series()[-1][1]
        acc_after = cs_after[-1][1] if cs_after else 0.0

        if prev_filtered is None:
            self._log(
                f"初回取得  生値={raw:.4f}%  平滑化={latest_filtered:.4f}%  {detail}"
            )
        else:
            acc_delta = acc_after - acc_before
            self._log(
                f"取得  生値={raw:.4f}%  平滑化={latest_filtered:.4f}%"
                f"  累積Δ={acc_delta:+.4f}%  累積={acc_after:.4f}%  {detail}"
            )

        self.status_changed.emit(f"最終取得: {now.strftime('%H:%M')}")
        self.save()
        self.updated.emit()

    def _on_sample_failed(self, msg: str) -> None:
        self._log(f"エラー  {msg}")
        self.status_changed.emit(f"⚠ {msg}")

    # ---------------------------------------------------------------- 派生計算
    def _filtered_series(self) -> list[tuple[_dt, float]]:
        """生値系列に対して直近 MEDIAN_WINDOW の中央値を取った平滑化系列。

        各点 i における値は raw_samples[max(0, i-W+1) .. i] の中央値。
        窓が埋まりきらない序盤は手持ちサンプルのみで中央値を取る（弱いフィルタ）。
        """
        out: list[tuple[_dt, float]] = []
        rs = self._raw_samples
        for i in range(len(rs)):
            start = max(0, i - MEDIAN_WINDOW + 1)
            window = [v for _, v in rs[start:i + 1]]
            out.append((rs[i][0], statistics.median(window)))
        return out

    def _cumulative_series(self) -> list[tuple[_dt, float]]:
        """平滑化系列の隣接差分を積み上げた累積系列。

        - delta が LVUP_DROP_THRESHOLD を超えて負 → LvUP 扱いで `(100-prev)+curr` を加算
        - delta が正 → そのまま加算
        - delta が小さく負（フィルタ後の微揺らぎ等）→ 加算しない（据置）
        """
        fs = self._filtered_series()
        if not fs:
            return []
        out: list[tuple[_dt, float]] = [(fs[0][0], 0.0)]
        acc = 0.0
        for i in range(1, len(fs)):
            delta = fs[i][1] - fs[i - 1][1]
            if delta < -LVUP_DROP_THRESHOLD:
                acc += (100.0 - fs[i - 1][1]) + fs[i][1]
            elif delta > 0:
                acc += delta
            out.append((fs[i][0], acc))
        return out

    # ---------------------------------------------------------------- 互換プロパティ
    @property
    def samples(self) -> list[tuple[_dt, float]]:
        """累積系列。外部表示用（len/最終時刻参照を含む既存呼び出しを維持）。"""
        return self._cumulative_series()

    @property
    def prev_raw(self) -> float | None:
        """最新の平滑化値（生値ではなく中央値フィルタ後）。表示用。"""
        fs = self._filtered_series()
        return fs[-1][1] if fs else None

    @property
    def accumulated(self) -> float:
        cs = self._cumulative_series()
        return cs[-1][1] if cs else 0.0

    # ---------------------------------------------------------------- 表示計算
    def current_speed(self) -> float | None:
        """直近の隣接Δレート（%/h換算）の中央値。

        - LvUP境界は `(100-prev)+curr` に補正
        - 微小マイナス（フィルタ残差ノイズ）は 0 として扱う
        - サンプル間隔のばらつきに対しても dt で正規化済みなので頑健
        """
        fs = self._filtered_series()
        if len(fs) < CURRENT_WINDOW:
            return None
        n_pairs = min(CURRENT_DELTA_SAMPLES, len(fs) - 1)
        if n_pairs < 1:
            return None
        rates: list[float] = []
        for i in range(len(fs) - n_pairs, len(fs)):
            dt_s = (fs[i][0] - fs[i - 1][0]).total_seconds()
            if dt_s <= 0:
                continue
            delta = fs[i][1] - fs[i - 1][1]
            if delta < -LVUP_DROP_THRESHOLD:
                delta = (100.0 - fs[i - 1][1]) + fs[i][1]
            elif delta < 0:
                delta = 0.0
            rates.append(delta / dt_s * 3600)
        if not rates:
            return None
        return statistics.median(rates)

    def avg_speed(self) -> float | None:
        return calc_speed(self._cumulative_series())

    def eta_to_levelup(self) -> tuple[float | None, float | None]:
        """(現在速度ベース分, 平均速度ベース分) — 不明は None。"""
        latest = self.prev_raw
        if latest is None:
            return None, None
        remaining = 100.0 - latest
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
