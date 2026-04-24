"""nightcrows シーンエディタ メインウィンドウ（タブ構成のシェル）。"""
from __future__ import annotations

import sys
import threading

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

import subprocess
from .adb import adb_disconnect, adb_ping, connect_usb, discover_and_connect, is_usb_serial, launch_scrcpy
from .connection_diag_dialog import ConnectionDiagDialog
from .flow_editor import FlowEditorWidget
from .maintenance_dialog import MaintenanceDialog
from .runner_widget import RunnerWidget
from .scene_editor import SceneEditorWidget
from .watcher_editor import WatcherEditorWidget
from .settings import AppSettings, load_settings, save_settings
from .settings_dialog import DeviceSettingsDialog

_WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


class MainWindow(QMainWindow):
    connect_result_signal = Signal(bool, str, str)   # ok, serial, message
    scrcpy_exited_signal  = Signal(int)              # exit code

    def __init__(self) -> None:
        super().__init__()
        self.resize(1280, 860)

        self.settings: AppSettings = load_settings()
        self._apply_tesseract_cmd(self.settings.tesseract_cmd)
        self.current_serial: str | None = None
        self.connect_stop = threading.Event()
        self.scrcpy_proc: subprocess.Popen | None = None

        self._build_ui()
        self._reload_device_combo()
        self._update_title()

        self.connect_result_signal.connect(self._on_connect_result)
        self.scrcpy_exited_signal.connect(self._on_scrcpy_exited)
        self.scene_editor.scene_path_changed.connect(self._on_scene_path_changed)

        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(1000)
        self._tick_clock()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)

        # 上部：デバイス接続バー
        bar = QHBoxLayout()
        bar.addWidget(QLabel("デバイス:"))
        self.device_combo = QComboBox()
        bar.addWidget(self.device_combo, 1)
        self.btn_connect = QPushButton("接続")
        self.btn_connect.clicked.connect(self._adb_connect)
        bar.addWidget(self.btn_connect)
        self.btn_disconnect = QPushButton("切断")
        self.btn_disconnect.setEnabled(False)
        self.btn_disconnect.clicked.connect(self._adb_disconnect)
        bar.addWidget(self.btn_disconnect)
        self.btn_scrcpy = QPushButton("scrcpy 起動")
        self.btn_scrcpy.clicked.connect(self._toggle_scrcpy)
        bar.addWidget(self.btn_scrcpy)
        btn_diag = QPushButton("📶 診断")
        btn_diag.clicked.connect(self._open_diag)
        bar.addWidget(btn_diag)
        btn_maintenance = QPushButton("🔧 メンテ")
        btn_maintenance.clicked.connect(self._open_maintenance)
        bar.addWidget(btn_maintenance)
        btn_settings = QPushButton("⚙")
        btn_settings.setFixedWidth(36)
        btn_settings.clicked.connect(self._open_settings)
        bar.addWidget(btn_settings)
        root.addLayout(bar)

        # 時計 ＋ グローバルランナーコントロール
        clock_row = QHBoxLayout()
        self.clock_label = QLabel()
        self.clock_label.setStyleSheet("padding-left: 4px; color: #333;")
        clock_row.addWidget(self.clock_label)
        clock_row.addStretch()
        self.btn_run = QPushButton("▶ 開始")
        self.btn_run.setFixedWidth(80)
        self.btn_run.setStyleSheet(
            "QPushButton{background:#388e3c;color:white;font-weight:bold;}"
            "QPushButton:hover{background:#2e7d32;}"
            "QPushButton:disabled{background:#aaa;}"
        )
        self.btn_run_stop = QPushButton("■ 停止")
        self.btn_run_stop.setFixedWidth(80)
        self.btn_run_stop.setEnabled(False)
        self.btn_run_stop.setStyleSheet(
            "QPushButton{background:#c62828;color:white;font-weight:bold;}"
            "QPushButton:hover{background:#b71c1c;}"
            "QPushButton:disabled{background:#aaa;}"
        )
        self.runner_status_label = QLabel("停止中")
        self.runner_status_label.setStyleSheet("color:#555; min-width:160px;")
        self.btn_run.clicked.connect(self._global_run_start)
        self.btn_run_stop.clicked.connect(self._global_run_stop)
        clock_row.addWidget(self.btn_run)
        clock_row.addWidget(self.btn_run_stop)
        clock_row.addWidget(self.runner_status_label)
        root.addLayout(clock_row)

        self.status_label = QLabel("未接続")
        self.status_label.setStyleSheet("color: #000; padding-left: 4px;")
        root.addWidget(self.status_label)

        # タブ
        self.tabs = QTabWidget()
        self.scene_editor = SceneEditorWidget(self)
        self.flow_editor = FlowEditorWidget(self)
        self.watcher_editor = WatcherEditorWidget(self)
        self.runner = RunnerWidget(self)
        self.tabs.addTab(self.scene_editor, "シーン編集")
        self.tabs.addTab(self.flow_editor, "フロー編集")
        self.tabs.addTab(self.watcher_editor, "ウォッチャー")
        self.tabs.addTab(self.runner, "実行ログ")
        root.addWidget(self.tabs, 1)

        self.runner.state_changed.connect(self._on_runner_state_changed)
        self.watcher_editor.watchers_changed.connect(self.flow_editor.refresh_watcher_tags)

        self.setCentralWidget(central)

    # ----------------------------------------------------------- exposed API
    def current_ip(self) -> str:
        """シーンエディタ等から呼ぶ、選択中デバイスの IP。"""
        return self.device_combo.currentData() or ""

    def set_connected(self, serial: str | None) -> None:
        """scrcpy 停止検知時など、外部から接続状態を落としたい時に呼ぶ。"""
        self._set_connected(serial)

    def select_device_by_ip(self, ip: str) -> None:
        idx = self.device_combo.findData(ip)
        if idx >= 0:
            self.device_combo.setCurrentIndex(idx)

    # ---------------------------------------------------------------- runner
    def _global_run_start(self) -> None:
        self.runner.start()

    def _global_run_stop(self) -> None:
        self.runner.stop()

    def _on_runner_state_changed(self, is_running: bool, label: str) -> None:
        self.btn_run.setEnabled(not is_running)
        self.btn_run_stop.setEnabled(is_running)
        self.runner_status_label.setText(label)
        self.runner_status_label.setStyleSheet(
            "color:#1b5e20; font-weight:bold; min-width:160px;"
            if is_running else
            "color:#555; min-width:160px;"
        )

    # ---------------------------------------------------------------- title
    def _tick_clock(self) -> None:
        from datetime import datetime as _dt
        now = _dt.now()
        wd = _WEEKDAYS[now.weekday()]
        self.clock_label.setText(
            now.strftime(f"%Y-%m-%d（{wd}）%H:%M:%S")
        )

    def _toggle_scrcpy(self) -> None:
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            proc = self.scrcpy_proc
            self.scrcpy_proc = None     # 先に None にして監視スレッドの誤通知を防ぐ
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass
            self.btn_scrcpy.setText("scrcpy 起動")
            self.scene_editor._log("scrcpy 停止")
            if self.current_serial and not adb_ping(self.current_serial, timeout=2):
                self.scene_editor._log(f"  adb 応答なし -> 未接続扱いに: {self.current_serial}")
                self._set_connected(None)
            return
        if not self.current_serial:
            QMessageBox.information(self, "情報", "先にデバイスに接続してください")
            return
        try:
            self.scrcpy_proc = launch_scrcpy(self.current_serial)
            self.btn_scrcpy.setText("scrcpy 停止")
            self.scene_editor._log(f"scrcpy 起動: {self.current_serial}")
            self._start_scrcpy_monitor()
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"scrcpy 起動失敗: {e}")

    def _start_scrcpy_monitor(self) -> None:
        """scrcpy プロセスの終了を別スレッドで監視し、シグナル経由で UI に通知する。"""
        proc = self.scrcpy_proc

        def _watch():
            code = proc.wait()          # プロセスが終わるまでブロック
            if self.scrcpy_proc is proc:    # まだ同じプロセスを管理中なら通知
                self.scrcpy_exited_signal.emit(code)

        threading.Thread(target=_watch, daemon=True).start()

    def _on_scrcpy_exited(self, code: int) -> None:
        """scrcpy が（外部から）終了したときの処理。"""
        if self.scrcpy_proc is None:
            return   # ユーザーが停止ボタンで止めた場合は既に None になっている
        self.scrcpy_proc = None
        self.btn_scrcpy.setText("scrcpy 起動")
        self.scene_editor._log(f"scrcpy が終了しました (exit={code})")
        if self.current_serial and not adb_ping(self.current_serial, timeout=2):
            self.scene_editor._log(f"  adb 応答なし -> 未接続扱いに: {self.current_serial}")
            self._set_connected(None)

    def _open_diag(self) -> None:
        if not self.current_serial:
            QMessageBox.information(self, "情報", "先にデバイスに接続してください")
            return
        ConnectionDiagDialog(self.current_serial, parent=self).exec()

    def _open_maintenance(self) -> None:
        MaintenanceDialog(parent=self).exec()

    def _update_title(self) -> None:
        name = self.scene_editor.scene.name or "(無題)"
        path = self.scene_editor.current_scene_path or "未保存"
        self.setWindowTitle(f"nightcrows — {name} — {path}")

    def _on_scene_path_changed(self, _path: str) -> None:
        self._update_title()

    # ----------------------------------------------------- connection state
    def _set_connected(self, serial: str | None) -> None:
        self.current_serial = serial
        if serial:
            self.status_label.setText(f"✓ 接続中: {serial}")
            self.status_label.setStyleSheet("color: #1b5e20; padding-left: 4px;")
            self.btn_connect.setEnabled(True)
            self.btn_connect.setStyleSheet(
                "QPushButton { background-color: #388e3c; color: white; font-weight: bold; }"
                "QPushButton:hover { background-color: #2e7d32; }"
            )
            self.btn_disconnect.setEnabled(True)
            self.btn_disconnect.setStyleSheet(
                "QPushButton { background-color: #c62828; color: white; font-weight: bold; }"
                "QPushButton:hover { background-color: #b71c1c; }"
            )
        else:
            self.status_label.setText("未接続")
            self.status_label.setStyleSheet("color: #000; padding-left: 4px;")
            self.btn_connect.setEnabled(True)
            self.btn_connect.setStyleSheet(
                "QPushButton { background-color: #c62828; color: white; font-weight: bold; }"
                "QPushButton:hover { background-color: #b71c1c; }"
            )
            self.btn_disconnect.setEnabled(False)
            self.btn_disconnect.setStyleSheet("")

    # --------------------------------------------------------- device combo
    def _reload_device_combo(self) -> None:
        prev_ip = self.current_ip() or self.settings.last_device
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for d in self.settings.devices:
            self.device_combo.addItem(f"{d.label}  ({d.ip})", d.ip)
        if prev_ip:
            idx = self.device_combo.findData(prev_ip)
            if idx >= 0:
                self.device_combo.setCurrentIndex(idx)
        self.device_combo.blockSignals(False)

    def _adb_connect(self) -> None:
        # 接続試行中なら「キャンセル」として動作
        if getattr(self, "_connecting", False):
            self.connect_stop.set()
            self.scene_editor._log("接続キャンセル要求")
            self.btn_connect.setEnabled(False)
            return

        ip = self.current_ip()
        if not ip:
            QMessageBox.information(self, "情報", "デバイスを選択してください")
            return

        self._connecting = True
        self.connect_stop.clear()
        self.scene_editor._log(f"接続試行: {ip}")
        self.btn_connect.setText("✕ キャンセル")
        self.btn_connect.setStyleSheet(
            "QPushButton { background-color: #f57c00; color: white; font-weight: bold; }"
            "QPushButton:hover { background-color: #e65100; }"
        )
        self.btn_disconnect.setEnabled(False)
        self.status_label.setText(f"… 接続試行中: {ip}")
        self.status_label.setStyleSheet("color: #f57c00; padding-left: 4px;")

        def run():
            if is_usb_serial(ip):
                ok, serial, msg = connect_usb(ip, log_fn=self.scene_editor._log)
            else:
                ok, serial, msg = discover_and_connect(
                    ip, log_fn=self.scene_editor._log,
                    should_stop=self.connect_stop.is_set,
                )
            self.connect_result_signal.emit(ok, serial, msg)

        threading.Thread(target=run, daemon=True).start()

    def _adb_disconnect(self) -> None:
        serial = self.current_serial
        if not serial:
            return
        self.connect_stop.set()
        self.scene_editor._log(f"adb disconnect {serial}...")
        self.btn_disconnect.setEnabled(False)

        def run():
            ok, out = adb_disconnect(serial)
            self.connect_result_signal.emit(ok, "", out)

        threading.Thread(target=run, daemon=True).start()

    def _on_connect_result(self, ok: bool, serial: str, msg: str) -> None:
        self._connecting = False
        self.btn_connect.setText("接続")
        prefix = "✓" if ok else "✗"
        self.scene_editor._log(f"  {prefix} {msg}")
        if ok and serial:
            self._set_connected(serial)
            # 最後の接続先を保存
            ip = self.current_ip()
            if ip and self.settings.last_device != ip:
                self.settings.last_device = ip
                save_settings(self.settings)
        else:
            self._set_connected(None)

    @staticmethod
    def _apply_tesseract_cmd(cmd: str) -> None:
        try:
            import pytesseract
            if cmd:
                pytesseract.pytesseract.tesseract_cmd = cmd
        except ImportError:
            pass

    def _open_settings(self) -> None:
        dlg = DeviceSettingsDialog(self.settings, parent=self)
        if dlg.exec() == QDialog.Accepted:
            new_settings = dlg.result_settings()
            if new_settings is None:
                return
            self.settings = new_settings
            save_settings(self.settings)
            self._apply_tesseract_cmd(self.settings.tesseract_cmd)
            self._reload_device_combo()
            self.scene_editor._log("設定を更新")

    # ------------------------------------------------------------ shutdown
    def closeEvent(self, event):
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            try:
                self.scrcpy_proc.terminate()
            except Exception:
                pass
        self.scene_editor.shutdown()
        self.runner.shutdown()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    # 日本語がくっきり出る太めの UI フォント
    font = QFont("Meiryo UI", 11)
    font.setWeight(QFont.Medium)
    app.setFont(font)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
