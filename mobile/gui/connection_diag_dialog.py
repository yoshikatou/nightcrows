"""接続診断ダイアログ。

ADB ラウンドトリップ遅延・WiFi 信号強度・スクショ速度を計測して表示する。
"""
from __future__ import annotations

import re
import subprocess
import threading
import time

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from .adb import ADB


class ConnectionDiagDialog(QDialog):
    _log_signal = Signal(str)
    _done_signal = Signal()

    def __init__(self, serial: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("接続診断")
        self.setMinimumSize(540, 420)
        self._serial = serial
        self._running = False

        lay = QVBoxLayout(self)

        lay.addWidget(QLabel(f"デバイス: <b>{serial}</b>"))

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet("font-family: 'Consolas', monospace; font-size: 10px;")
        lay.addWidget(self._log, 1)

        btn_row = QHBoxLayout()
        self._btn_run = QPushButton("▶ 診断開始")
        self._btn_run.clicked.connect(self._run)
        btn_row.addWidget(self._btn_run)
        btn_row.addStretch()
        btn_close = QPushButton("閉じる")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)

        self._log_signal.connect(self._append)
        self._done_signal.connect(self._on_done)

    def _append(self, text: str) -> None:
        self._log.appendPlainText(text)
        self._log.verticalScrollBar().setValue(
            self._log.verticalScrollBar().maximum()
        )

    def _run(self) -> None:
        if self._running:
            return
        self._running = True
        self._btn_run.setEnabled(False)
        self._log.clear()
        threading.Thread(target=self._diagnose, daemon=True).start()

    def _on_done(self) -> None:
        self._running = False
        self._btn_run.setEnabled(True)

    def _shell(self, *args, timeout: float = 6.0) -> str:
        r = subprocess.run(
            [ADB, "-s", self._serial, "shell"] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )
        return (r.stdout or "").strip()

    # ------------------------------------------------------------------
    def _diagnose(self) -> None:
        log = self._log_signal.emit

        log("=" * 52)
        log("【1】ADB ラウンドトリップ遅延（10回計測）")
        log("=" * 52)
        samples: list[float] = []
        for i in range(10):
            t0 = time.perf_counter()
            try:
                r = subprocess.run(
                    [ADB, "-s", self._serial, "shell", "echo", "ok"],
                    capture_output=True, timeout=5,
                )
                ok = r.returncode == 0 and b"ok" in (r.stdout or b"")
            except Exception:
                ok = False
            elapsed = (time.perf_counter() - t0) * 1000
            mark = "✓" if ok else "✗"
            samples.append(elapsed if ok else 9999)
            log(f"  {mark} #{i+1:2d}  {elapsed:6.1f} ms")

        valid = [s for s in samples if s < 9999]
        if valid:
            log(f"\n  最小: {min(valid):.1f} ms  平均: {sum(valid)/len(valid):.1f} ms  最大: {max(valid):.1f} ms")
            avg = sum(valid) / len(valid)
            if avg < 30:
                log("  → 良好 ✓")
            elif avg < 100:
                log("  → やや遅め（WiFi 混雑 or 距離）")
            else:
                log("  → 遅い（スクショ取得・タップ応答に影響あり）")
        else:
            log("  → 全て失敗（接続確認してください）")

        log("")
        log("=" * 52)
        log("【2】WiFi 信号強度・リンク速度")
        log("=" * 52)
        try:
            wifi_out = self._shell("dumpsys", "wifi")
            rssi = None
            link_speed = None
            freq = None
            ssid = None
            for line in wifi_out.splitlines():
                if "rssi" in line.lower() and rssi is None:
                    m = re.search(r"rssi[=\s:]+(-?\d+)", line, re.I)
                    if m:
                        rssi = int(m.group(1))
                if "linkspeed" in line.lower() and link_speed is None:
                    m = re.search(r"linkspeed[=\s:]+(\d+)", line, re.I)
                    if m:
                        link_speed = int(m.group(1))
                if "freq" in line.lower() and freq is None:
                    m = re.search(r"freq[=\s:]+(\d+)", line, re.I)
                    if m:
                        freq = int(m.group(1))
                if ("ssid=" in line or "SSID:" in line) and ssid is None:
                    m = re.search(r'SSID[=:\s]+"?([^",\s]+)', line, re.I)
                    if m:
                        ssid = m.group(1)

            if rssi is not None:
                if rssi >= -60:
                    grade = "良好 ✓"
                elif rssi >= -70:
                    grade = "普通"
                elif rssi >= -80:
                    grade = "弱め — 距離を縮めると改善"
                else:
                    grade = "非常に弱い"
                log(f"  RSSI       : {rssi} dBm  ({grade})")
            else:
                log("  RSSI       : 取得できませんでした")

            if link_speed is not None:
                log(f"  リンク速度 : {link_speed} Mbps")
            if freq is not None:
                band = "5 GHz" if freq >= 5000 else "2.4 GHz"
                log(f"  周波数帯   : {freq} MHz ({band})")
                if freq < 5000:
                    log("  → 2.4 GHz は混雑しやすいため 5 GHz 帯を推奨")
            if ssid:
                log(f"  SSID       : {ssid}")
        except Exception as e:
            log(f"  取得失敗: {e}")

        log("")
        log("=" * 52)
        log("【3】スクショ取得速度")
        log("=" * 52)
        try:
            times: list[float] = []
            for i in range(3):
                t0 = time.perf_counter()
                r = subprocess.run(
                    [ADB, "-s", self._serial, "exec-out", "screencap", "-p"],
                    capture_output=True, timeout=15,
                )
                elapsed = (time.perf_counter() - t0) * 1000
                size_kb = len(r.stdout) / 1024
                times.append(elapsed)
                log(f"  #{i+1}  {elapsed:6.0f} ms  ({size_kb:.0f} KB)")
            avg = sum(times) / len(times)
            log(f"\n  平均: {avg:.0f} ms")
            if avg < 500:
                log("  → 良好 ✓")
            elif avg < 1500:
                log("  → やや遅め（ウォッチャー判定に若干影響）")
            else:
                log("  → 遅い（ウォッチャーの反応が鈍くなります）")
        except Exception as e:
            log(f"  取得失敗: {e}")

        log("")
        log("=" * 52)
        log("【4】デバイス情報")
        log("=" * 52)
        try:
            model   = self._shell("getprop", "ro.product.model")
            android = self._shell("getprop", "ro.build.version.release")
            size    = self._shell("wm", "size")
            log(f"  モデル      : {model}")
            log(f"  Android     : {android}")
            log(f"  画面解像度  : {size}")
        except Exception as e:
            log(f"  取得失敗: {e}")

        log("")
        log("診断完了")
        self._done_signal.emit()
