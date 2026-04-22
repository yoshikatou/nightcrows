"""ランナータブ（最小限）。

フローファイルを選択して再生・停止できる。
ステータス表示と手動トリガは 4b〜5 で拡充。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from .flow import load_flow
from .flow_runner import replay_flow

FLOWS_DIR = "flows"


class RunnerWidget(QWidget):
    log_signal = Signal(str)
    flow_finished = Signal()

    def __init__(self, main_window) -> None:
        super().__init__()
        self._mw = main_window
        self.flow_thread: threading.Thread | None = None
        self.flow_stop = threading.Event()

        self._build_ui()

        self.log_signal.connect(self._append_log)
        self.flow_finished.connect(self._on_flow_finished)

        os.makedirs(FLOWS_DIR, exist_ok=True)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)

        # フロー選択
        row = QHBoxLayout()
        row.addWidget(QLabel("フロー:"))
        self.flow_path_edit = QLineEdit()
        self.flow_path_edit.setReadOnly(True)
        self.flow_path_edit.setPlaceholderText("flows/ 以下の .json を選択")
        row.addWidget(self.flow_path_edit, 1)
        btn_browse = QPushButton("選択...")
        btn_browse.clicked.connect(self._browse_flow)
        row.addWidget(btn_browse)
        lay.addLayout(row)

        # ステータス
        self.status_label = QLabel("停止中")
        lay.addWidget(self.status_label)

        # 開始/停止
        row2 = QHBoxLayout()
        self.btn_start = QPushButton("▶ 開始")
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = QPushButton("■ 停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        row2.addWidget(self.btn_start)
        row2.addWidget(self.btn_stop)
        lay.addLayout(row2)

        # ログ
        lay.addWidget(QLabel("ログ:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        lay.addWidget(self.log_view, 1)

    # --------------------------------------------------------------- actions
    def _browse_flow(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "フロー選択", FLOWS_DIR, "JSON (*.json)",
        )
        if path:
            self.flow_path_edit.setText(path)

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_signal.emit(f"[{ts}] {msg}")

    def _append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def _start(self) -> None:
        flow_path = self.flow_path_edit.text().strip()
        if not flow_path:
            QMessageBox.information(self, "情報", "フローファイルを選択してください")
            return
        serial = self._mw.current_serial
        if not serial:
            QMessageBox.information(self, "情報", "先にデバイスに『接続』してください")
            return
        try:
            flow = load_flow(flow_path)
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"フロー読込失敗: {e}")
            return

        self.flow_stop.clear()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.status_label.setText(f"実行中: {flow.name}")
        self._log(f"フロー開始: {flow.name}  main_sequence={len(flow.main_sequence)} 件, "
                  f"schedule={len(flow.schedule)} 件, watchers={len(flow.watchers)} 件")

        def run() -> None:
            try:
                replay_flow(
                    flow, serial,
                    log=self._log,
                    should_stop=self.flow_stop.is_set,
                )
            except Exception as e:
                self._log(f"エラー: {e}")
            finally:
                self.flow_finished.emit()

        self.flow_thread = threading.Thread(target=run, daemon=True)
        self.flow_thread.start()

    def _stop(self) -> None:
        self.flow_stop.set()
        self._log("停止要求")

    def _on_flow_finished(self) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status_label.setText("停止中")

    # ------------------------------------------------------------ shutdown
    def shutdown(self) -> None:
        self.flow_stop.set()
