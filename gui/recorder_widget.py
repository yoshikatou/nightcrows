"""録画タブ — 一定間隔で画面スナップショットを保存する UI。"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, time as dtime

from PySide6.QtCore import QTime, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTimeEdit, QVBoxLayout,
    QWidget,
)

from .screen_recorder import (
    ScreenRecorder, cleanup_old_folders, folder_size_bytes, human_bytes,
)
from .settings import RecordingSettings, save_settings


class RecorderWidget(QWidget):
    state_changed = Signal(bool)   # True=録画開始, False=停止

    def __init__(self, main_window) -> None:
        super().__init__()
        self._mw = main_window
        self._recorder: ScreenRecorder | None = None

        self._build_ui()
        self._load_from_settings()

        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._refresh_status)
        self._ui_timer.start(1000)
        self._refresh_status()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        hint = QLabel(
            "デバイスに接続後、間隔・出力先を指定して開始してください。"
            "  日付フォルダごとに JPEG で保存されます。"
        )
        hint.setStyleSheet("color:#666; font-size:11px;")
        lay.addWidget(hint)

        form = QFormLayout()

        out_row = QHBoxLayout()
        self.out_edit = QLineEdit()
        out_row.addWidget(self.out_edit, 1)
        btn_pick = QPushButton("参照")
        btn_pick.clicked.connect(self._pick_folder)
        out_row.addWidget(btn_pick)
        btn_open = QPushButton("フォルダを開く")
        btn_open.clicked.connect(self._open_folder)
        out_row.addWidget(btn_open)
        form.addRow("出力フォルダ:", out_row)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 60)
        self.interval_spin.setSuffix(" 分")
        form.addRow("撮影間隔:", self.interval_spin)

        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(50, 95)
        self.quality_spin.setSuffix(" (JPEG)")
        form.addRow("画質:", self.quality_spin)

        stop_row = QHBoxLayout()
        self.auto_stop_chk = QCheckBox("自動停止")
        self.auto_stop_time = QTimeEdit()
        self.auto_stop_time.setDisplayFormat("HH:mm")
        stop_row.addWidget(self.auto_stop_chk)
        stop_row.addWidget(self.auto_stop_time)
        stop_row.addWidget(QLabel("（指定時刻に達したら停止）"))
        stop_row.addStretch()
        form.addRow("", stop_row)

        del_row = QHBoxLayout()
        self.auto_del_chk = QCheckBox("古いフォルダを自動削除")
        self.auto_del_days = QSpinBox()
        self.auto_del_days.setRange(1, 365)
        self.auto_del_days.setSuffix(" 日より古い")
        del_row.addWidget(self.auto_del_chk)
        del_row.addWidget(self.auto_del_days)
        del_row.addWidget(QLabel("（録画開始時にチェック）"))
        del_row.addStretch()
        form.addRow("", del_row)

        lay.addLayout(form)

        btn_row = QHBoxLayout()
        self.btn_start = QPushButton("● 録画開始")
        self.btn_start.setStyleSheet(
            "QPushButton{background:#c62828;color:white;font-weight:bold;padding:6px 16px;}"
            "QPushButton:hover{background:#b71c1c;}"
            "QPushButton:disabled{background:#aaa;}"
        )
        self.btn_stop = QPushButton("■ 停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(
            "QPushButton{background:#37474f;color:white;font-weight:bold;padding:6px 16px;}"
            "QPushButton:hover{background:#263238;}"
            "QPushButton:disabled{background:#aaa;}"
        )
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        self.status_label = QLabel("待機中")
        self.status_label.setStyleSheet(
            "font-size:13px; color:#222; padding:6px; background:#eceff1; border-radius:4px;"
        )
        lay.addWidget(self.status_label)

        lay.addWidget(QLabel("ログ:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        lay.addWidget(self.log_view, 1)

    # ----------------------------------------------------------- settings
    def _load_from_settings(self) -> None:
        s = self._mw.settings.recording
        self.out_edit.setText(s.out_dir)
        self.interval_spin.setValue(s.interval_min)
        self.quality_spin.setValue(s.jpeg_quality)
        self.auto_stop_chk.setChecked(s.auto_stop_enabled)
        try:
            hh, mm = [int(x) for x in s.auto_stop_time.split(":")]
            self.auto_stop_time.setTime(QTime(hh, mm))
        except (ValueError, AttributeError):
            self.auto_stop_time.setTime(QTime(8, 0))
        self.auto_del_chk.setChecked(s.auto_delete_enabled)
        self.auto_del_days.setValue(s.auto_delete_days)

    def _save_to_settings(self) -> None:
        t = self.auto_stop_time.time()
        self._mw.settings.recording = RecordingSettings(
            out_dir=self.out_edit.text().strip() or "recordings",
            interval_min=self.interval_spin.value(),
            jpeg_quality=self.quality_spin.value(),
            auto_stop_enabled=self.auto_stop_chk.isChecked(),
            auto_stop_time=f"{t.hour():02d}:{t.minute():02d}",
            auto_delete_enabled=self.auto_del_chk.isChecked(),
            auto_delete_days=self.auto_del_days.value(),
        )
        save_settings(self._mw.settings)

    # ----------------------------------------------------------- folder
    def _pick_folder(self) -> None:
        cur = self.out_edit.text() or "recordings"
        path = QFileDialog.getExistingDirectory(self, "出力フォルダ", cur)
        if path:
            self.out_edit.setText(path)

    def _open_folder(self) -> None:
        path = self.out_edit.text() or "recordings"
        os.makedirs(path, exist_ok=True)
        try:
            os.startfile(path)
        except Exception:
            try:
                subprocess.Popen(["explorer", path])
            except Exception as e:
                QMessageBox.warning(self, "エラー", f"フォルダを開けません: {e}")

    # ----------------------------------------------------------- log
    def _log(self, msg: str) -> None:
        self.log_view.appendPlainText(f"[{datetime.now():%H:%M:%S}] {msg}")

    # ----------------------------------------------------------- record (public)
    def is_recording(self) -> bool:
        return self._recorder is not None and self._recorder.is_running()

    def start_recording(self) -> bool:
        """外部からも呼べる録画開始。成功時 True / 失敗時 False。"""
        if self.is_recording():
            return True
        if not self._mw.current_serial:
            QMessageBox.information(self, "情報", "先にデバイスに接続してください")
            return False
        self._save_to_settings()
        s = self._mw.settings.recording
        out_dir = s.out_dir or "recordings"
        os.makedirs(out_dir, exist_ok=True)

        if s.auto_delete_enabled:
            cleanup_old_folders(out_dir, s.auto_delete_days, log_fn=self._log)

        auto_stop = None
        if s.auto_stop_enabled:
            try:
                hh, mm = [int(x) for x in s.auto_stop_time.split(":")]
                auto_stop = dtime(hh, mm)
            except ValueError:
                pass

        self._recorder = ScreenRecorder(
            serial=self._mw.current_serial,
            out_dir=out_dir,
            interval_s=s.interval_min * 60,
            jpeg_quality=s.jpeg_quality,
            auto_stop_at=auto_stop,
            log_fn=self._log,
        )
        self._recorder.start()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._set_inputs_enabled(False)
        self._log(
            f"録画開始: {out_dir}  間隔 {s.interval_min}分  品質 {s.jpeg_quality}"
            + (f"  自動停止 {s.auto_stop_time}" if s.auto_stop_enabled else "")
        )
        self.state_changed.emit(True)
        return True

    def stop_recording(self) -> None:
        """外部からも呼べる録画停止。"""
        was_running = self.is_recording()
        if self._recorder is not None:
            self._recorder.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._set_inputs_enabled(True)
        if was_running:
            self.state_changed.emit(False)

    # 内部UIハンドラは public 版を呼ぶだけ
    def _start(self) -> None:
        self.start_recording()

    def _stop(self) -> None:
        self.stop_recording()

    def _set_inputs_enabled(self, enabled: bool) -> None:
        self.out_edit.setEnabled(enabled)
        self.interval_spin.setEnabled(enabled)
        self.quality_spin.setEnabled(enabled)
        self.auto_stop_chk.setEnabled(enabled)
        self.auto_stop_time.setEnabled(enabled)
        self.auto_del_chk.setEnabled(enabled)
        self.auto_del_days.setEnabled(enabled)

    def _refresh_status(self) -> None:
        r = self._recorder
        if r is None:
            total = folder_size_bytes(self.out_edit.text() or "recordings")
            self.status_label.setText(f"待機中 — 累計 {human_bytes(total)}")
            return
        if not r.is_running():
            # 自動停止 or 終了検出
            if not self.btn_start.isEnabled():
                self.btn_start.setEnabled(True)
                self.btn_stop.setEnabled(False)
                self._set_inputs_enabled(True)
                self.state_changed.emit(False)
            total = folder_size_bytes(self.out_edit.text() or "recordings")
            self.status_label.setText(f"待機中 — 累計 {human_bytes(total)}")
            return
        last = os.path.basename(r.last_path) if r.last_path else "—"
        self.status_label.setText(
            f"● 録画中 / 撮影 {r.count} 枚 / セッション {human_bytes(r.total_bytes)} "
            f"/ 最終: {last}"
        )

    # ----------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        if self._recorder is not None:
            self._recorder.stop()
