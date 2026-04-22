"""nightcrows シーンエディタ メインウィンドウ（タブ構成のシェル）。"""
from __future__ import annotations

import sys
import threading

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from .adb import adb_disconnect, discover_and_connect
from .runner_widget import RunnerWidget
from .scene_editor import SceneEditorWidget
from .settings import AppSettings, load_settings, save_settings
from .settings_dialog import DeviceSettingsDialog


class MainWindow(QMainWindow):
    connect_result_signal = Signal(bool, str, str)   # ok, serial, message

    def __init__(self) -> None:
        super().__init__()
        self.resize(1280, 860)

        self.settings: AppSettings = load_settings()
        self.current_serial: str | None = None
        self.connect_stop = threading.Event()

        self._build_ui()
        self._reload_device_combo()
        self._update_title()

        self.connect_result_signal.connect(self._on_connect_result)
        self.scene_editor.scene_path_changed.connect(self._on_scene_path_changed)

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
        btn_settings = QPushButton("⚙")
        btn_settings.setFixedWidth(36)
        btn_settings.clicked.connect(self._open_settings)
        bar.addWidget(btn_settings)
        root.addLayout(bar)

        self.status_label = QLabel("未接続")
        self.status_label.setStyleSheet("color: #000; padding-left: 4px;")
        root.addWidget(self.status_label)

        # タブ
        self.tabs = QTabWidget()
        self.scene_editor = SceneEditorWidget(self)
        self.runner = RunnerWidget(self)
        self.tabs.addTab(self.scene_editor, "シーン編集")
        self.tabs.addTab(self._build_flow_placeholder(), "フロー編集")
        self.tabs.addTab(self.runner, "ランナー")
        root.addWidget(self.tabs, 1)

        self.setCentralWidget(central)

    def _build_flow_placeholder(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        label = QLabel("フロー編集（未実装）\n\n複数シーンの繋ぎ方・時刻スケジュール・監視ルールを編集するタブ")
        label.setStyleSheet("padding: 40px;")
        lay.addWidget(label)
        lay.addStretch(1)
        return w

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

    # ---------------------------------------------------------------- title
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
            self.btn_disconnect.setEnabled(True)
        else:
            self.status_label.setText("未接続")
            self.status_label.setStyleSheet("color: #000; padding-left: 4px;")
            self.btn_connect.setEnabled(True)
            self.btn_disconnect.setEnabled(False)

    # --------------------------------------------------------- device combo
    def _reload_device_combo(self) -> None:
        prev_ip = self.current_ip()
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
        ip = self.current_ip()
        if not ip:
            QMessageBox.information(self, "情報", "デバイスを選択してください")
            return
        self.connect_stop.clear()
        self.scene_editor._log(f"接続試行: {ip}")
        self.btn_connect.setEnabled(False)
        self.btn_disconnect.setEnabled(False)
        self.status_label.setText(f"… 接続試行中: {ip}")
        self.status_label.setStyleSheet("color: #f57c00; padding-left: 4px;")

        def run():
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
        prefix = "✓" if ok else "✗"
        self.scene_editor._log(f"  {prefix} {msg}")
        if ok and serial:
            self._set_connected(serial)
        else:
            self._set_connected(None)

    def _open_settings(self) -> None:
        dlg = DeviceSettingsDialog(self.settings, parent=self)
        if dlg.exec() == dlg.Accepted:
            new_settings = dlg.result_settings()
            if new_settings is None:
                return
            self.settings = new_settings
            save_settings(self.settings)
            self._reload_device_combo()
            self.scene_editor._log("デバイス設定を更新")

    # ------------------------------------------------------------ shutdown
    def closeEvent(self, event):
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
