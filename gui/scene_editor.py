"""シーン編集タブ。キャンバス / ステップ列 / 記録 / 再生 / ログを内包する。"""
from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from .adb import get_rotation_and_size, launch_scrcpy, adb_ping, screencap
from .canvas import SnapshotCanvas
from .recorder import TapRecorder
from .replay import replay_scene
from .scene import Scene, Step, load_scene, save_scene
from .scroll_dialog import ScrollDialog

TEMPLATES_DIR = "templates"
SNAPSHOTS_DIR = os.path.join(TEMPLATES_DIR, "snapshots")
SCENES_DIR = "scenes"


class SceneEditorWidget(QWidget):
    log_signal = Signal(str)
    replay_finished = Signal()
    tap_recorded_signal = Signal(int, int, int, float)
    rec_stopped_signal = Signal()
    rec_error_signal = Signal(str)
    scene_path_changed = Signal(str)  # MainWindow にタイトル更新を通知

    def __init__(self, main_window) -> None:
        super().__init__()
        self._mw = main_window
        self.scene = Scene()
        self.current_scene_path: str | None = None
        self.scrcpy_proc: subprocess.Popen | None = None
        self.replay_thread: threading.Thread | None = None
        self.replay_stop = threading.Event()
        self.recorder: TapRecorder | None = None
        self._pixmap_cache: dict[str, QPixmap] = {}

        self._build_ui()

        self.log_signal.connect(self._append_log)
        self.replay_finished.connect(self._on_replay_finished)
        self.tap_recorded_signal.connect(self._on_tap_recorded)
        self.rec_stopped_signal.connect(self._on_rec_stopped)
        self.rec_error_signal.connect(self._on_rec_error)

        os.makedirs(TEMPLATES_DIR, exist_ok=True)
        os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
        os.makedirs(SCENES_DIR, exist_ok=True)

        self._refresh_canvas_view()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QHBoxLayout(self)

        self.canvas = SnapshotCanvas()
        self.canvas.clicked.connect(self._on_canvas_click)
        self.canvas.region_selected.connect(self._on_canvas_region)
        root.addWidget(self.canvas, 3)

        right = QVBoxLayout()

        # シーン名
        row_sn = QHBoxLayout()
        row_sn.addWidget(QLabel("シーン名:"))
        self.scene_name_edit = QLineEdit(self.scene.name)
        self.scene_name_edit.textChanged.connect(self._on_scene_name_changed)
        row_sn.addWidget(self.scene_name_edit, 1)
        right.addLayout(row_sn)

        # シーンファイル
        row3 = QHBoxLayout()
        btn_new = QPushButton("新規")
        btn_new.clicked.connect(self._new_scene)
        btn_save = QPushButton("保存")
        btn_save.clicked.connect(self._save_scene)
        btn_save_as = QPushButton("別名保存")
        btn_save_as.clicked.connect(self._save_scene_as)
        btn_load = QPushButton("読込")
        btn_load.clicked.connect(self._load_scene)
        for b in (btn_new, btn_save, btn_save_as, btn_load):
            row3.addWidget(b)
        right.addLayout(row3)

        # scrcpy / スナップ更新
        row2 = QHBoxLayout()
        self.btn_scrcpy = QPushButton("scrcpy 起動")
        self.btn_scrcpy.clicked.connect(self._toggle_scrcpy)
        row2.addWidget(self.btn_scrcpy)
        btn_snap_update = QPushButton("スナップ更新")
        btn_snap_update.clicked.connect(self._add_snapshot_step)
        row2.addWidget(btn_snap_update)
        right.addLayout(row2)

        # 記録
        row_rec = QHBoxLayout()
        self.btn_rec_start = QPushButton("● 記録開始")
        self.btn_rec_start.clicked.connect(self._start_recording)
        self.btn_rec_stop = QPushButton("■ 記録停止")
        self.btn_rec_stop.clicked.connect(self._stop_recording)
        self.btn_rec_stop.setEnabled(False)
        row_rec.addWidget(self.btn_rec_start)
        row_rec.addWidget(self.btn_rec_stop)
        right.addLayout(row_rec)

        # ステップ一覧
        right.addWidget(QLabel("ステップ（行選択でスナップ切替・マーカー強調）:"))
        self.step_list = QListWidget()
        self.step_list.currentRowChanged.connect(self._on_step_row_changed)
        right.addWidget(self.step_list, 2)

        row4 = QHBoxLayout()
        btn_add_wait = QPushButton("固定待ち 1s 追加")
        btn_add_wait.clicked.connect(self._add_wait_fixed)
        btn_add_scroll = QPushButton("自動スクロール追加")
        btn_add_scroll.clicked.connect(self._add_scroll)
        btn_del = QPushButton("選択ステップ削除")
        btn_del.clicked.connect(self._del_step)
        row4.addWidget(btn_add_wait)
        row4.addWidget(btn_add_scroll)
        row4.addWidget(btn_del)
        right.addLayout(row4)

        # 再生
        row5 = QHBoxLayout()
        self.btn_replay = QPushButton("再生")
        self.btn_replay.clicked.connect(self._start_replay)
        self.btn_stop_replay = QPushButton("停止")
        self.btn_stop_replay.clicked.connect(self._stop_replay)
        self.btn_stop_replay.setEnabled(False)
        row5.addWidget(self.btn_replay)
        row5.addWidget(self.btn_stop_replay)
        right.addLayout(row5)

        right.addWidget(QLabel("ログ:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        right.addWidget(self.log_view, 1)

        root.addLayout(right, 2)

    # -------------------------------------------------------------- helpers
    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_signal.emit(f"[{ts}] {msg}")

    def _append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def _current_ip(self) -> str:
        return self._mw.current_ip()

    def _current_serial(self) -> str:
        return self._mw.current_serial or ""

    def _require_connected(self) -> str | None:
        s = self._mw.current_serial
        if not s:
            QMessageBox.information(self, "情報",
                                    "先にデバイスに『接続』してください")
            return None
        return s

    # ----------------------------------------------------------- scene name
    def _on_scene_name_changed(self, text: str) -> None:
        self.scene.name = text
        self.scene_path_changed.emit(self.current_scene_path or "")

    # -------------------------------------------------------------- scrcpy
    def _toggle_scrcpy(self) -> None:
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            try:
                self.scrcpy_proc.terminate()
                try:
                    self.scrcpy_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.scrcpy_proc.kill()
            except Exception:
                pass
            self.scrcpy_proc = None
            self.btn_scrcpy.setText("scrcpy 起動")
            self._log("scrcpy 停止")
            # scrcpy 停止で adb 接続が巻き添えで死んでいる場合があるので検証
            cur = self._mw.current_serial
            if cur and not adb_ping(cur, timeout=2):
                self._log(f"  adb 応答なし -> 未接続扱いに: {cur}")
                self._mw.set_connected(None)
            return
        serial = self._require_connected()
        if not serial:
            return
        try:
            self.scrcpy_proc = launch_scrcpy(serial)
            self.btn_scrcpy.setText("scrcpy 停止")
            self._log(f"scrcpy 起動: {serial}")
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"scrcpy 起動失敗: {e}")

    # ---------------------------------------------------------- snapshot step
    def _add_snapshot_step(self) -> None:
        serial = self._require_connected()
        if not serial:
            return
        try:
            rot, pw, ph = get_rotation_and_size(serial)
            png = screencap(serial)
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"スナップショット失敗: {e}")
            return

        os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
        name = f"snap_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
        path = os.path.join(SNAPSHOTS_DIR, name).replace("\\", "/")
        with open(path, "wb") as f:
            f.write(png)

        self.scene.rotation = rot
        self.scene.phys_size = (pw, ph)

        step = Step(type="snapshot", params={"path": path})
        self.scene.steps.append(step)
        self._refresh_step_list(select_last=True)
        self._log(f"スナップ追加: {path}")

    # ------------------------------------------------------------ canvas ev
    def _on_canvas_click(self, x: int, y: int) -> None:
        step = Step(type="tap", params={"x": x, "y": y, "duration_ms": 100})
        self.scene.steps.append(step)
        self._refresh_step_list(select_last=True)
        self._log(f"tap 追加: ({x},{y})")

    def _on_canvas_region(self, x: int, y: int, w: int, h: int) -> None:
        pm = self.canvas.current_pixmap()
        if pm is None:
            QMessageBox.information(self, "情報",
                                    "スナップショットがありません。先に『スナップ更新』を押してください")
            return
        os.makedirs(TEMPLATES_DIR, exist_ok=True)
        name = f"tpl_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
        path = os.path.join(TEMPLATES_DIR, name).replace("\\", "/")
        if not pm.copy(x, y, w, h).save(path):
            QMessageBox.warning(self, "エラー", "テンプレート保存失敗")
            return
        step = Step(type="wait_image", params={
            "template": path,
            "region": [x, y, w, h],
            "threshold": 0.85,
            "timeout_s": 30,
        })
        self.scene.steps.append(step)
        self._refresh_step_list(select_last=True)
        self._log(f"wait_image 追加: {path} region=[{x},{y},{w},{h}]")

    # ---------------------------------------------------------------- steps
    def _add_wait_fixed(self) -> None:
        step = Step(type="wait_fixed", params={"seconds": 1.0})
        self.scene.steps.append(step)
        self._refresh_step_list(select_last=True)
        self._log("wait_fixed 追加: 1.0s")

    def _add_scroll(self) -> None:
        pm = self.canvas.current_pixmap()
        if pm is not None and not pm.isNull():
            lw, lh = pm.width(), pm.height()
        else:
            lw, lh = self.scene.logical_size if self.scene.logical_size else (0, 0)
        dlg = ScrollDialog(lw, lh, parent=self)
        if dlg.exec() != dlg.Accepted:
            return
        params = dlg.get_params()
        self.scene.steps.append(Step(type="scroll", params=params))
        self._refresh_step_list(select_last=True)
        self._log(f"scroll 追加: ({params['x1']},{params['y1']})"
                  f"->({params['x2']},{params['y2']}) "
                  f"{params['duration_ms']}±{params['duration_jitter_ms']}ms")

    def _del_step(self) -> None:
        row = self.step_list.currentRow()
        if row < 0 or row >= len(self.scene.steps):
            return
        removed = self.scene.steps.pop(row)
        self._refresh_step_list()
        self._log(f"削除: {removed.type}")

    def _refresh_step_list(self, select_last: bool = False) -> None:
        self.step_list.blockSignals(True)
        self.step_list.clear()
        for i, s in enumerate(self.scene.steps):
            if s.type == "tap":
                label = f"{i + 1}. tap ({s.params.get('x')},{s.params.get('y')}) {s.params.get('duration_ms')}ms"
            elif s.type == "snapshot":
                label = f"{i + 1}. 📷 snapshot  {os.path.basename(s.params.get('path', ''))}"
            elif s.type == "wait_fixed":
                label = f"{i + 1}. wait {s.params.get('seconds')}s"
            elif s.type == "wait_image":
                label = f"{i + 1}. wait_image  {os.path.basename(s.params.get('template', ''))}"
            elif s.type == "swipe":
                label = (f"{i + 1}. swipe ({s.params.get('x1')},{s.params.get('y1')})"
                         f"->({s.params.get('x2')},{s.params.get('y2')}) "
                         f"{s.params.get('duration_ms')}ms")
            elif s.type == "scroll":
                p = s.params
                label = (f"{i + 1}. scroll "
                         f"({p.get('x1')}±{p.get('x1_jitter',0)},"
                         f"{p.get('y1')}±{p.get('y1_jitter',0)})"
                         f"→({p.get('x2')}±{p.get('x2_jitter',0)},"
                         f"{p.get('y2')}±{p.get('y2_jitter',0)}) "
                         f"{p.get('duration_ms')}±{p.get('duration_jitter_ms',0)}ms")
            else:
                label = f"{i + 1}. {s.type}  {s.params}"
            self.step_list.addItem(label)
        if select_last and self.scene.steps:
            self.step_list.setCurrentRow(len(self.scene.steps) - 1)
        self.step_list.blockSignals(False)
        self._refresh_canvas_view()

    def _on_step_row_changed(self, row: int) -> None:
        self._refresh_canvas_view()

    # ----------------------------------------------------------- canvas view
    def _compute_view(self, selected_idx: int | None) -> tuple[str | None, list[tuple[int, int, int, bool]]]:
        snapshots: list[str] = []
        group: list[int] = []
        current = -1
        for s in self.scene.steps:
            if s.type == "snapshot":
                snapshots.append(s.params.get("path", ""))
                current = len(snapshots) - 1
            group.append(current)

        if not snapshots:
            return None, []

        if selected_idx is not None and 0 <= selected_idx < len(self.scene.steps):
            display_group = group[selected_idx]
            highlight_idx = selected_idx
        else:
            display_group = len(snapshots) - 1
            highlight_idx = None

        if display_group < 0:
            return None, []

        markers: list[tuple[int, int, int, bool]] = []
        n = 0
        for i, s in enumerate(self.scene.steps):
            if group[i] != display_group or s.type != "tap":
                continue
            n += 1
            hi = (i == highlight_idx)
            markers.append((n, int(s.params.get("x", 0)), int(s.params.get("y", 0)), hi))

        return snapshots[display_group], markers

    def _refresh_canvas_view(self) -> None:
        row = self.step_list.currentRow()
        sel = row if row >= 0 else None
        path, markers = self._compute_view(sel)
        if path is None:
            self.canvas.set_snapshot(None)
            self.canvas.set_markers([])
            return
        pm = self._pixmap_cache.get(path)
        if pm is None or pm.isNull():
            pm = QPixmap(path)
            if not pm.isNull():
                self._pixmap_cache[path] = pm
        self.canvas.set_snapshot(pm if not pm.isNull() else None)
        self.canvas.set_markers(markers)

    # -------------------------------------------------------------- recording
    def _start_recording(self) -> None:
        serial = self._require_connected()
        if not serial:
            return
        if self.recorder and self.recorder.is_running():
            return

        if not any(s.type == "snapshot" for s in self.scene.steps):
            r = QMessageBox.question(
                self, "確認",
                "スナップショットが1枚もありません。\n"
                "このまま記録すると座標は保存されますが、後で位置確認ができません。\n\n"
                "『スナップ更新』を押してから記録開始することを推奨します。\n"
                "このまま続行しますか？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if r != QMessageBox.Yes:
                return

        self.recorder = TapRecorder(serial)
        self.recorder.tap_detected.connect(self.tap_recorded_signal.emit)
        self.recorder.stopped.connect(self.rec_stopped_signal.emit)
        self.recorder.error.connect(self.rec_error_signal.emit)
        self.recorder.start()

        self.btn_rec_start.setEnabled(False)
        self.btn_rec_stop.setEnabled(True)
        self._log("タップ記録開始")

    def _stop_recording(self) -> None:
        if self.recorder:
            self.recorder.stop()
            self._log("停止要求")

    def _on_tap_recorded(self, x: int, y: int, duration_ms: int, gap_s: float) -> None:
        if gap_s > 0.01:
            self.scene.steps.append(Step(type="wait_fixed",
                                         params={"seconds": round(gap_s, 3)}))
        self.scene.steps.append(Step(type="tap", params={
            "x": int(x), "y": int(y), "duration_ms": int(duration_ms),
        }))
        self._refresh_step_list(select_last=True)
        self._log(f"記録: tap ({x},{y}) dur={duration_ms}ms gap={gap_s:.2f}s")

    def _on_rec_stopped(self) -> None:
        self.btn_rec_start.setEnabled(True)
        self.btn_rec_stop.setEnabled(False)
        self._log("タップ記録終了")

    def _on_rec_error(self, msg: str) -> None:
        self._log(f"記録エラー: {msg}")
        QMessageBox.warning(self, "記録エラー", msg)

    # ----------------------------------------------------------- scene file
    def _new_scene(self) -> None:
        if self.scene.steps and not self._confirm_discard():
            return
        self.scene = Scene()
        self.current_scene_path = None
        self._pixmap_cache.clear()
        self.scene_name_edit.setText(self.scene.name)
        self._refresh_step_list()
        self.scene_path_changed.emit("")
        self._log("新規シーン")

    def _confirm_discard(self) -> bool:
        r = QMessageBox.question(
            self, "確認",
            "現在のシーンを破棄して新規作成しますか？",
            QMessageBox.Yes | QMessageBox.No,
        )
        return r == QMessageBox.Yes

    def _save_scene(self) -> None:
        if self.current_scene_path:
            self._write_scene(self.current_scene_path)
        else:
            self._save_scene_as()

    def _save_scene_as(self) -> None:
        default = os.path.join(SCENES_DIR, f"{self.scene.name or 'scene'}.json")
        path, _ = QFileDialog.getSaveFileName(self, "別名保存", default, "JSON (*.json)")
        if not path:
            return
        self._write_scene(path)

    def _write_scene(self, path: str) -> None:
        self.scene.device_ip = self._current_ip()
        try:
            save_scene(self.scene, path)
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"保存失敗: {e}")
            return
        self.current_scene_path = path
        self.scene_path_changed.emit(path)
        self._log(f"保存: {path}")

    def _load_scene(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "読込", SCENES_DIR, "JSON (*.json)")
        if not path:
            return
        try:
            self.scene = load_scene(path)
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"読み込み失敗: {e}")
            return
        self.current_scene_path = path
        self._pixmap_cache.clear()
        self.scene_name_edit.setText(self.scene.name)
        if self.scene.device_ip:
            self._mw.select_device_by_ip(self.scene.device_ip)
        self._refresh_step_list()
        self.scene_path_changed.emit(path)
        self._log(f"読込: {path} ({len(self.scene.steps)} 件)")

    # --------------------------------------------------------------- replay
    def _start_replay(self) -> None:
        if self.replay_thread and self.replay_thread.is_alive():
            return
        if not self.scene.steps:
            QMessageBox.information(self, "情報", "ステップが空です")
            return
        serial = self._require_connected()
        if not serial:
            return
        self.replay_stop.clear()
        self.btn_replay.setEnabled(False)
        self.btn_stop_replay.setEnabled(True)

        def run():
            try:
                replay_scene(
                    self.scene, serial,
                    log=self._log,
                    should_stop=self.replay_stop.is_set,
                )
            except Exception as e:
                self._log(f"エラー: {e}")
            finally:
                self.replay_finished.emit()

        self.replay_thread = threading.Thread(target=run, daemon=True)
        self.replay_thread.start()

    def _stop_replay(self) -> None:
        self.replay_stop.set()
        self._log("停止要求")

    def _on_replay_finished(self) -> None:
        self.btn_replay.setEnabled(True)
        self.btn_stop_replay.setEnabled(False)

    # ------------------------------------------------------------ shutdown
    def shutdown(self) -> None:
        """MainWindow の closeEvent から呼ぶ。"""
        self.replay_stop.set()
        if self.recorder:
            self.recorder.stop()
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            try:
                self.scrcpy_proc.terminate()
            except Exception:
                pass
