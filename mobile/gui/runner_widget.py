"""ランナータブ — 実行ログの表示専用。再生/停止はメインウィンドウ上部のボタンで行う。"""
from __future__ import annotations

import os
import threading
from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QLabel, QMessageBox, QPlainTextEdit, QVBoxLayout, QWidget,
)

from .flow import load_flow
from .flow_runner import replay_flow
from .maintenance import load_maintenance
from .notify import show_desktop_alert

FLOWS_DIR = "flows"
LOG_DIR = "logs"


class RunnerWidget(QWidget):
    log_signal    = Signal(str)
    flow_finished = Signal()
    state_changed = Signal(bool, str)   # is_running, status_text

    def __init__(self, main_window) -> None:
        super().__init__()
        self._mw = main_window
        self.flow_thread: threading.Thread | None = None
        self.flow_stop = threading.Event()
        self._log_fh = None      # 現在書き込み中のファイルハンドル
        self._log_date = ""      # _log_fh に対応する日付 (YYYY-MM-DD)

        self._build_ui()
        self.log_signal.connect(self._append_log)
        self.flow_finished.connect(self._on_flow_finished)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        self.status_label = QLabel("停止中")
        lay.addWidget(self.status_label)
        lay.addWidget(QLabel("ログ:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        lay.addWidget(self.log_view, 1)

    # --------------------------------------------------------------- public API
    def start(self) -> None:
        """メインウィンドウの ▶ ボタンから呼ばれる。"""
        if self.flow_thread and self.flow_thread.is_alive():
            return
        flow_path = getattr(self._mw.flow_editor, "_flow_path", None)
        if not flow_path:
            QMessageBox.information(
                self._mw, "情報", "フロー編集タブでフローを開いてください"
            )
            return
        serial = self._mw.current_serial
        if not serial:
            QMessageBox.information(
                self._mw, "情報", "先にデバイスに『接続』してください"
            )
            return
        try:
            flow = load_flow(flow_path)
        except Exception as e:
            QMessageBox.critical(self._mw, "エラー", f"フロー読込失敗: {e}")
            return

        self.flow_stop.clear()
        self.status_label.setText(f"実行中: {flow.name}")
        self.state_changed.emit(True, f"実行中: {flow.name}")
        self._log(
            f"フロー開始: {flow.name}  "
            f"schedule={len(flow.schedule)} 件, watchers={len(flow.watchers)} 件"
        )

        maintenance = load_maintenance()
        if maintenance:
            self._log(f"メンテナンス登録: {len(maintenance)} 件")

        global_watchers = self._mw.watcher_editor.get_watchers()
        if global_watchers:
            self._log(f"グローバルウォッチャー: {len(global_watchers)} 件")
        flow.watchers = global_watchers + flow.watchers

        def run() -> None:
            try:
                replay_flow(
                    flow, serial,
                    log=self._log,
                    should_stop=self.flow_stop.is_set,
                    maintenance=maintenance,
                    notify_fn=show_desktop_alert,
                )
            except Exception as e:
                self._log(f"エラー: {e}")
            finally:
                self.flow_finished.emit()

        self.flow_thread = threading.Thread(target=run, daemon=True)
        self.flow_thread.start()

    def stop(self) -> None:
        """メインウィンドウの ■ ボタンから呼ばれる。"""
        self.flow_stop.set()
        self._log("停止要求")

    def run_scenes_now(self, scenes: list[str]) -> None:
        """フロー実行とは独立して、指定シーンを即座に順番実行する。"""
        if self.flow_thread and self.flow_thread.is_alive():
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self._mw, "情報", "実行中です。停止してから使用してください")
            return
        serial = self._mw.current_serial
        if not serial:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self._mw, "情報", "先にデバイスに接続してください")
            return

        import os
        from .replay import replay_scene
        from .scene import load_scene

        def _resolve(path: str) -> str:
            return path if os.path.isabs(path) else os.path.join("scenes", path)

        self.flow_stop.clear()
        names = ", ".join(os.path.basename(s) for s in scenes)
        self.status_label.setText(f"即時実行: {names}")
        self.state_changed.emit(True, f"即時実行: {names}")
        self._log(f"▶ 即時実行: {scenes}")

        def run() -> None:
            seq_state: dict = {}
            try:
                for path in scenes:
                    if self.flow_stop.is_set():
                        break
                    try:
                        scene = load_scene(_resolve(path))
                    except Exception as e:
                        self._log(f"  シーン読込失敗: {path}: {e}")
                        continue
                    replay_scene(scene, serial,
                                 log=self._log,
                                 should_stop=self.flow_stop.is_set,
                                 _seq_state=seq_state)
            except Exception as e:
                self._log(f"エラー: {e}")
            finally:
                self.flow_finished.emit()

        self.flow_thread = threading.Thread(target=run, daemon=True)
        self.flow_thread.start()

    # --------------------------------------------------------------- internal
    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_signal.emit(f"[{ts}] {msg}")

    def _append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)
        self._write_to_file(line)

    _LOG_RETAIN_DAYS = 30   # この日数より古いログファイルを自動削除

    def _write_to_file(self, line: str) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._log_date != today or self._log_fh is None:
            self._close_log_file()
            try:
                os.makedirs(LOG_DIR, exist_ok=True)
                path = os.path.join(LOG_DIR, f"{today}.log")
                self._log_fh = open(path, "a", encoding="utf-8")
                self._log_date = today
                self._purge_old_logs()
            except Exception:
                return
        try:
            self._log_fh.write(line + "\n")
            self._log_fh.flush()
        except Exception:
            pass

    def _purge_old_logs(self) -> None:
        """_LOG_RETAIN_DAYS 日より古い .log ファイルを削除する。"""
        try:
            from datetime import timedelta
            cutoff = datetime.now() - timedelta(days=self._LOG_RETAIN_DAYS)
            for fname in os.listdir(LOG_DIR):
                if not fname.endswith(".log"):
                    continue
                stem = fname[:-4]   # "YYYY-MM-DD"
                try:
                    file_date = datetime.strptime(stem, "%Y-%m-%d")
                except ValueError:
                    continue
                if file_date < cutoff:
                    try:
                        os.remove(os.path.join(LOG_DIR, fname))
                    except Exception:
                        pass
        except Exception:
            pass

    def _close_log_file(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None
            self._log_date = ""

    def _on_flow_finished(self) -> None:
        self.status_label.setText("停止中")
        self.state_changed.emit(False, "停止中")

    # ------------------------------------------------------------ shutdown
    def shutdown(self) -> None:
        self.flow_stop.set()
        self._close_log_file()
