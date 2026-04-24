"""シーン編集タブ。キャンバス / ステップ列 / 記録 / 再生 / ログを内包する。"""
from __future__ import annotations

import os
import threading
from datetime import datetime

from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from .adb import get_rotation_and_size, screencap
from .canvas import SnapshotCanvas
from .recorder import TapRecorder
from .replay import replay_scene
from .scene import Scene, Step, load_scene, save_scene
from .scroll_dialog import ScrollDialog

TEMPLATES_DIR = "templates"
SNAPSHOTS_DIR = os.path.join(TEMPLATES_DIR, "snapshots")
SCENES_DIR = "scenes"

_KEYEVENTS: list[tuple[str, str]] = [
    ("戻る (BACK)",               "KEYCODE_BACK"),
    ("ホーム (HOME)",              "KEYCODE_HOME"),
    ("タスク切替 (APP_SWITCH)",    "KEYCODE_APP_SWITCH"),
    ("メニュー (MENU)",            "KEYCODE_MENU"),
    ("決定 (ENTER)",               "KEYCODE_ENTER"),
    ("バックスペース (DEL)",        "KEYCODE_DEL"),
    ("音量UP (VOLUME_UP)",         "KEYCODE_VOLUME_UP"),
    ("音量DOWN (VOLUME_DOWN)",     "KEYCODE_VOLUME_DOWN"),
    ("電源 (POWER)",               "KEYCODE_POWER"),
    ("通知パネル (NOTIFICATION)",  "KEYCODE_NOTIFICATION"),
]


# ------------------------------------------------------------------ クリッカブル画像ラベル
class _ClickableImageLabel(QLabel):
    """スナップショットを表示し、クリックすると論理座標を通知するラベル。"""
    clicked = Signal(int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.CrossCursor)
        self._pm: QPixmap | None = None
        self._region: list[int] | None = None
        self._then_taps: list[tuple[int, int]] = []
        self._else_taps: list[tuple[int, int]] = []

    def set_pixmap(self, pm: QPixmap) -> None:
        self._pm = pm
        self._update_display()

    def set_region(self, region: list[int] | None) -> None:
        self._region = region
        self._update_display()

    def set_branch_markers(self,
                           then_taps: list[tuple[int, int]],
                           else_taps: list[tuple[int, int]]) -> None:
        self._then_taps = list(then_taps)
        self._else_taps = list(else_taps)
        self._update_display()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_display()

    def _update_display(self) -> None:
        if not self._pm or self._pm.isNull():
            return
        scaled = self._pm.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        iw, ih = self._pm.width(), self._pm.height()
        sw, sh = scaled.width(), scaled.height()
        sx = sw / iw if iw else 1.0
        sy = sh / ih if ih else 1.0

        needs_overlay = (
            (self._region and len(self._region) == 4)
            or self._then_taps or self._else_taps
        )
        if not needs_overlay:
            super().setPixmap(scaled)
            return

        composite = QPixmap(scaled)
        p = QPainter(composite)

        if self._region and len(self._region) == 4:
            rx, ry, rw, rh = self._region
            p.setBrush(QColor(21, 101, 192, 45))
            p.setPen(QPen(QColor("#1565C0"), 2, Qt.DashLine))
            p.drawRect(QRectF(rx * sx, ry * sy, rw * sx, rh * sy))

        for markers, fill, prefix in [
            (self._then_taps, QColor("#1B5E20"), "✓"),
            (self._else_taps, QColor("#B71C1C"), "✗"),
        ]:
            for idx, (lx, ly) in enumerate(markers):
                wx, wy = lx * sx, ly * sy
                r = 12
                p.setBrush(fill)
                p.setPen(QPen(QColor("white"), 1.5))
                p.drawEllipse(QRectF(wx - r, wy - r, r * 2, r * 2))
                font = QFont(); font.setBold(True); font.setPointSize(8)
                p.setFont(font)
                p.setPen(QColor("white"))
                p.drawText(
                    QRectF(wx - r, wy - r, r * 2, r * 2),
                    Qt.AlignCenter, f"{prefix}{idx + 1}"
                )

        p.end()
        super().setPixmap(composite)

    def mousePressEvent(self, event) -> None:
        if self._pm is None or self._pm.isNull():
            return
        lw, lh = self.width(), self.height()
        iw, ih = self._pm.width(), self._pm.height()
        if iw == 0 or ih == 0:
            return
        scale = min(lw / iw, lh / ih)
        disp_w, disp_h = iw * scale, ih * scale
        ox, oy = (lw - disp_w) / 2, (lh - disp_h) / 2
        px = event.position().x() - ox
        py = event.position().y() - oy
        if 0 <= px <= disp_w and 0 <= py <= disp_h:
            self.clicked.emit(int(px / scale), int(py / scale))


# ------------------------------------------------------------------ 分岐ステップエディタ
class _SimpleBranchEditor(QWidget):
    """if_image の then/else 各ブランチのステップを編集するウィジェット。"""
    steps_changed = Signal()

    def __init__(self, label: str, steps: list[dict], parent=None) -> None:
        super().__init__(parent)
        self._steps: list[dict] = [dict(s) for s in steps]

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(label))

        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.currentRowChanged.connect(self._on_sel)
        lay.addWidget(self.list, 1)

        add_row = QHBoxLayout()
        for lbl, fn in [("👆 タップ", self._add_tap),
                         ("⏱ 待ち", self._add_wait),
                         ("🔑 キー", self._add_key)]:
            b = QPushButton(lbl); b.clicked.connect(fn); add_row.addWidget(b)
        add_row.addStretch()
        lay.addLayout(add_row)

        ctrl = QHBoxLayout()
        self.btn_up   = QPushButton("↑")
        self.btn_down = QPushButton("↓")
        self.btn_del  = QPushButton("✕")
        self.btn_up.clicked.connect(self._up)
        self.btn_down.clicked.connect(self._down)
        self.btn_del.clicked.connect(self._delete)
        for b in (self.btn_up, self.btn_down, self.btn_del):
            ctrl.addWidget(b)
        ctrl.addStretch()
        lay.addLayout(ctrl)
        self._refresh()

    def _label(self, s: dict) -> str:
        t, p = s.get("type", ""), s.get("params", {})
        if t == "tap":      return f"👆 タップ ({p.get('x')}, {p.get('y')})"
        if t == "wait_fixed": return f"⏱ 待ち {p.get('seconds')}s"
        if t == "keyevent": return f"🔑 {next((l for l,c in _KEYEVENTS if c==p.get('keycode')), p.get('keycode',''))}"
        return f"{t} {p}"

    def _refresh(self) -> None:
        row = self.list.currentRow()
        self.list.clear()
        for s in self._steps:
            self.list.addItem(self._label(s))
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)
        self._on_sel(self.list.currentRow())

    def _on_sel(self, row: int) -> None:
        n = self.list.count()
        self.btn_up.setEnabled(row > 0)
        self.btn_down.setEnabled(0 <= row < n - 1)
        self.btn_del.setEnabled(row >= 0)

    def _add_tap(self) -> None:
        dlg = QDialog(self); dlg.setWindowTitle("タップ座標")
        lay = QVBoxLayout(dlg); form = QFormLayout(); lay.addLayout(form)
        xs = QSpinBox(); xs.setRange(0, 9999); xs.setValue(540)
        ys = QSpinBox(); ys.setRange(0, 9999); ys.setValue(960)
        form.addRow("X:", xs); form.addRow("Y:", ys)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject); lay.addWidget(bb)
        if dlg.exec() == QDialog.Accepted:
            self._steps.append({"type": "tap", "params": {"x": xs.value(), "y": ys.value(), "duration_ms": 100}})
            self._refresh(); self.list.setCurrentRow(len(self._steps) - 1)
            self.steps_changed.emit()

    def _add_wait(self) -> None:
        secs, ok = QInputDialog.getDouble(self, "固定待ち", "秒数:", 1.0, 0.1, 3600.0, 1)
        if ok:
            self._steps.append({"type": "wait_fixed", "params": {"seconds": secs}})
            self._refresh(); self.list.setCurrentRow(len(self._steps) - 1)

    def _add_key(self) -> None:
        dlg = QDialog(self); dlg.setWindowTitle("キーイベント")
        lay = QVBoxLayout(dlg)
        cb = QComboBox()
        for lbl, kc in _KEYEVENTS:
            cb.addItem(lbl, kc)
        lay.addWidget(cb)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject); lay.addWidget(bb)
        if dlg.exec() == QDialog.Accepted:
            self._steps.append({"type": "keyevent", "params": {"keycode": cb.currentData()}})
            self._refresh(); self.list.setCurrentRow(len(self._steps) - 1)

    def _up(self) -> None:
        r = self.list.currentRow()
        if r <= 0: return
        self._steps[r-1], self._steps[r] = self._steps[r], self._steps[r-1]
        self._refresh(); self.list.setCurrentRow(r - 1)
        self.steps_changed.emit()

    def _down(self) -> None:
        r = self.list.currentRow()
        if r < 0 or r >= len(self._steps) - 1: return
        self._steps[r], self._steps[r+1] = self._steps[r+1], self._steps[r]
        self._refresh(); self.list.setCurrentRow(r + 1)
        self.steps_changed.emit()

    def _delete(self) -> None:
        r = self.list.currentRow()
        if r < 0: return
        self._steps.pop(r); self._refresh()
        self.steps_changed.emit()

    def add_tap(self, x: int, y: int) -> None:
        """キャンバスクリックからタップステップを直接追加する。"""
        self._steps.append({"type": "tap", "params": {"x": x, "y": y, "duration_ms": 100}})
        self._refresh()
        self.list.setCurrentRow(len(self._steps) - 1)
        self.steps_changed.emit()

    def get_tap_positions(self) -> list[tuple[int, int]]:
        return [
            (s["params"]["x"], s["params"]["y"])
            for s in self._steps
            if s.get("type") == "tap"
        ]

    def get_steps(self) -> list[dict]:
        return list(self._steps)


# ------------------------------------------------------------------ パターン選択ダイアログ
class _PickSceneDialog(QDialog):
    """pick_scene ステップの編集ダイアログ。モード選択＋シーンリスト管理。"""

    def __init__(self, params: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("パターン選択 — シーンリスト")
        self.setMinimumWidth(480)
        import uuid as _uuid
        self._step_id: str = params.get("step_id") or _uuid.uuid4().hex[:8]
        self._scenes: list[str] = list(params.get("scenes") or [])

        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        # モード選択
        from PySide6.QtWidgets import QButtonGroup, QGroupBox, QRadioButton
        grp = QGroupBox("選択方法")
        grp_lay = QHBoxLayout(grp)
        self._rb_random = QRadioButton("🎲 ランダム（毎回ランダムに1つ選ぶ）")
        self._rb_seq    = QRadioButton("🔄 順番（1回目→A、2回目→B…と順番に選ぶ）")
        bg = QButtonGroup(self)
        bg.addButton(self._rb_random, 0)
        bg.addButton(self._rb_seq, 1)
        grp_lay.addWidget(self._rb_random)
        grp_lay.addWidget(self._rb_seq)
        if params.get("mode") == "sequential":
            self._rb_seq.setChecked(True)
        else:
            self._rb_random.setChecked(True)
        lay.addWidget(grp)

        # シーンリスト
        lay.addWidget(QLabel("シーン一覧（実行候補）:"))
        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        for s in self._scenes:
            self._list.addItem(os.path.basename(s))
            item = self._list.item(self._list.count() - 1)
            item.setData(Qt.UserRole, s)
        lay.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("＋ シーンを追加")
        btn_add.clicked.connect(self._add_scene)
        btn_del = QPushButton("✕ 削除")
        btn_del.clicked.connect(self._del_scene)
        btn_up  = QPushButton("↑")
        btn_up.clicked.connect(self._move_up)
        btn_dn  = QPushButton("↓")
        btn_dn.clicked.connect(self._move_down)
        for b in (btn_add, btn_del, btn_up, btn_dn):
            btn_row.addWidget(b)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        hint = QLabel("※ 順番モードでは、フロー実行中のカウンタが持続します（シーン再実行ごとに次へ進む）")
        hint.setStyleSheet("color: #666; font-size: 10px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _add_scene(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "シーン選択", SCENES_DIR, "JSON (*.json)")
        if not path:
            return
        rel = os.path.relpath(path, ".").replace("\\", "/")
        item = QListWidgetItem(os.path.basename(path))
        item.setData(Qt.UserRole, rel)
        self._list.addItem(item)

    def _del_scene(self) -> None:
        row = self._list.currentRow()
        if row >= 0:
            self._list.takeItem(row)

    def _move_up(self) -> None:
        row = self._list.currentRow()
        if row <= 0:
            return
        item = self._list.takeItem(row)
        self._list.insertItem(row - 1, item)
        self._list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self._list.currentRow()
        if row < 0 or row >= self._list.count() - 1:
            return
        item = self._list.takeItem(row)
        self._list.insertItem(row + 1, item)
        self._list.setCurrentRow(row + 1)

    def get_params(self) -> dict:
        mode = "sequential" if self._rb_seq.isChecked() else "random"
        scenes = [
            self._list.item(i).data(Qt.UserRole)
            for i in range(self._list.count())
        ]
        return {"mode": mode, "scenes": scenes, "step_id": self._step_id}


class _IfImageBranchDialog(QDialog):
    """if_image ステップの then/else ブランチをインラインで編集するダイアログ。"""

    def __init__(self, step_params: dict,
                 snapshot: QPixmap | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("if_image 分岐編集")
        self.setMinimumSize(960 if snapshot else 660, 560)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            f"テンプレート: {os.path.basename(step_params.get('template', ''))}\n"
            "各ブランチにステップを追加してください。ステップが空の場合はその分岐をスキップします。"
        ))

        main_sp = QSplitter(Qt.Horizontal)

        # スナップショットパネル（左）
        if snapshot and not snapshot.isNull():
            snap_w = QWidget()
            snap_lay = QVBoxLayout(snap_w)
            snap_lay.setContentsMargins(0, 0, 0, 0)
            snap_lay.addWidget(QLabel("📍 画像をクリック → then / else にタップ追加"))
            self._img_label = _ClickableImageLabel()
            self._img_label.set_pixmap(snapshot)
            self._img_label.set_region(step_params.get("region"))
            self._img_label.setMinimumWidth(220)
            self._img_label.clicked.connect(self._on_snapshot_click)
            snap_lay.addWidget(self._img_label, 1)
            main_sp.addWidget(snap_w)
        else:
            self._img_label = None

        # ブランチエディタ（右）
        branch_w = QWidget()
        branch_lay = QVBoxLayout(branch_w)
        branch_lay.setContentsMargins(0, 0, 0, 0)
        branch_sp = QSplitter(Qt.Horizontal)
        self._then_ed = _SimpleBranchEditor(
            "🟢 マッチした場合 (then)",
            step_params.get("then_steps") or []
        )
        self._else_ed = _SimpleBranchEditor(
            "🔴 マッチしなかった場合 (else)",
            step_params.get("else_steps") or []
        )
        branch_sp.addWidget(self._then_ed)
        branch_sp.addWidget(self._else_ed)
        branch_lay.addWidget(branch_sp, 1)
        main_sp.addWidget(branch_w)

        lay.addWidget(main_sp, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        if self._img_label is not None:
            self._then_ed.steps_changed.connect(self._refresh_branch_markers)
            self._else_ed.steps_changed.connect(self._refresh_branch_markers)
            self._refresh_branch_markers()

    def _refresh_branch_markers(self) -> None:
        if self._img_label is None:
            return
        self._img_label.set_branch_markers(
            self._then_ed.get_tap_positions(),
            self._else_ed.get_tap_positions(),
        )

    def _on_snapshot_click(self, x: int, y: int) -> None:
        menu = QMenu(self)
        act_then = menu.addAction(f"🟢 then に追加  ({x}, {y})")
        act_else = menu.addAction(f"🔴 else に追加  ({x}, {y})")
        action = menu.exec(QCursor.pos())
        if action == act_then:
            self._then_ed.add_tap(x, y)
        elif action == act_else:
            self._else_ed.add_tap(x, y)

    def get_then_steps(self) -> list[dict]: return self._then_ed.get_steps()
    def get_else_steps(self) -> list[dict]: return self._else_ed.get_steps()


class SceneEditorWidget(QWidget):
    log_signal = Signal(str)
    replay_finished = Signal()
    tap_recorded_signal = Signal(int, int, int, float)
    rec_stopped_signal = Signal()
    rec_error_signal = Signal(str)
    scene_path_changed = Signal(str)  # MainWindow にタイトル更新を通知
    match_result_signal = Signal(object)  # dict — マッチテスト結果
    step_highlight_signal = Signal(int)   # 再生中の現在ステップ index

    def __init__(self, main_window) -> None:
        super().__init__()
        self._mw = main_window
        self.scene = Scene()
        self.current_scene_path: str | None = None
        self.replay_thread: threading.Thread | None = None
        self.replay_stop = threading.Event()
        self.recorder: TapRecorder | None = None
        self._pixmap_cache: dict[str, QPixmap] = {}

        self._marker_step_indices: list[int] = []
        self._drag_start: tuple[int, int] | None = None   # ドラッグ始点待ち
        self._reselect_step_idx: int | None = None         # テンプレート再設定対象ステップ
        self._highlighted_row: int | None = None           # 再生ハイライト行

        self._build_ui()

        self.log_signal.connect(self._append_log)
        self.replay_finished.connect(self._on_replay_finished)
        self.tap_recorded_signal.connect(self._on_tap_recorded)
        self.rec_stopped_signal.connect(self._on_rec_stopped)
        self.rec_error_signal.connect(self._on_rec_error)
        self.match_result_signal.connect(self._on_match_result)
        self.step_highlight_signal.connect(self._on_step_highlight)

        os.makedirs(TEMPLATES_DIR, exist_ok=True)
        os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
        os.makedirs(SCENES_DIR, exist_ok=True)

        self._refresh_canvas_view()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QHBoxLayout(self)

        # ---- 左カラム: キャンバス（stretch）+ ログ（固定高） ----
        left = QVBoxLayout()
        self.canvas = SnapshotCanvas()
        self.canvas.clicked.connect(self._on_canvas_click)
        self.canvas.region_selected.connect(self._on_canvas_region)
        self.canvas.marker_moved.connect(self._on_marker_moved)
        self.canvas.right_clicked.connect(self._on_canvas_right_click)
        left.addWidget(self.canvas, 1)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        self.log_view.setFixedHeight(120)
        left.addWidget(self.log_view)
        root.addLayout(left, 3)

        # ---- 右カラム: 操作パネル ----
        right = QVBoxLayout()

        # シーン名
        row_sn = QHBoxLayout()
        row_sn.addWidget(QLabel("シーン名:"))
        self.scene_name_edit = QLineEdit(self.scene.name)
        self.scene_name_edit.textChanged.connect(self._on_scene_name_changed)
        row_sn.addWidget(self.scene_name_edit, 1)
        right.addLayout(row_sn)

        # シーンファイル操作
        row_file = QHBoxLayout()
        for label, slot in [("新規", self._new_scene), ("保存", self._save_scene),
                             ("別名保存", self._save_scene_as), ("読込", self._load_scene)]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            row_file.addWidget(b)
        right.addLayout(row_file)

        # スナップ更新 + 記録トグル
        row_snap = QHBoxLayout()
        btn_snap = QPushButton("📷 スナップ更新")
        btn_snap.clicked.connect(self._add_snapshot_step)
        row_snap.addWidget(btn_snap)
        self.btn_rec_toggle = QPushButton("● 記録開始")
        self.btn_rec_toggle.clicked.connect(self._toggle_recording)
        row_snap.addWidget(self.btn_rec_toggle)
        right.addLayout(row_snap)

        # ステップ一覧
        right.addWidget(QLabel("ステップ:"))
        self.step_list = QListWidget()
        self.step_list.setDragDropMode(QListWidget.InternalMove)
        self.step_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.step_list.model().rowsMoved.connect(self._on_steps_reordered)
        self.step_list.currentRowChanged.connect(self._on_step_row_changed)
        self.step_list.itemDoubleClicked.connect(self._on_step_double_clicked)
        right.addWidget(self.step_list, 1)

        # ステップ追加・削除
        row_add = QHBoxLayout()
        self._btn_drag = QPushButton("ドラッグ")
        self._btn_drag.setCheckable(True)
        self._btn_drag.clicked.connect(self._toggle_drag_mode)
        row_add.addWidget(self._btn_drag)
        for label, slot in [("⏱ 待ち", self._add_wait_fixed),
                             ("🔑 キー", self._add_keyevent),
                             ("↕ スクロール", self._add_scroll),
                             ("📂 取込", self._import_scene),
                             ("┄ グループ", self._add_group_header),
                             ("🎲 選択", self._add_pick_scene)]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            row_add.addWidget(b)
        right.addLayout(row_add)

        row_move = QHBoxLayout()
        btn_del = QPushButton("削除")
        btn_del.clicked.connect(self._del_step)
        btn_up = QPushButton("↑")
        btn_up.clicked.connect(self._move_step_up)
        btn_down = QPushButton("↓")
        btn_down.clicked.connect(self._move_step_down)
        for b in (btn_del, btn_up, btn_down):
            row_move.addWidget(b)
        row_move.addStretch()
        self.btn_run_step = QPushButton("▶ 1行実行")
        self.btn_run_step.setStyleSheet(
            "QPushButton { background:#1565c0; color:white; font-weight:bold; }"
            "QPushButton:hover { background:#0d47a1; }"
            "QPushButton:disabled { background:#aaa; }"
        )
        self.btn_run_step.clicked.connect(self._run_selected_step)
        row_move.addWidget(self.btn_run_step)
        self.btn_match_test = QPushButton("🔍 マッチテスト")
        self.btn_match_test.setEnabled(False)
        self.btn_match_test.setStyleSheet(
            "QPushButton { background:#00695c; color:white; font-weight:bold; }"
            "QPushButton:hover { background:#004d40; }"
            "QPushButton:disabled { background:#aaa; }"
        )
        self.btn_match_test.clicked.connect(self._test_match_step)
        row_move.addWidget(self.btn_match_test)
        self.btn_reselect = QPushButton("🖼 再設定")
        self.btn_reselect.setEnabled(False)
        self.btn_reselect.setCheckable(True)
        self.btn_reselect.setStyleSheet(
            "QPushButton { background:#6a1b9a; color:white; font-weight:bold; }"
            "QPushButton:hover { background:#4a148c; }"
            "QPushButton:checked { background:#e65100; color:white; font-weight:bold; }"
            "QPushButton:disabled { background:#aaa; }"
        )
        self.btn_reselect.clicked.connect(self._toggle_reselect_mode)
        row_move.addWidget(self.btn_reselect)
        right.addLayout(row_move)

        # 再生
        row_play = QHBoxLayout()
        self.btn_replay = QPushButton("▶ 再生")
        self.btn_replay.clicked.connect(self._start_replay)
        self.btn_stop_replay = QPushButton("■ 停止")
        self.btn_stop_replay.clicked.connect(self._stop_replay)
        self.btn_stop_replay.setEnabled(False)
        row_play.addWidget(self.btn_replay)
        row_play.addWidget(self.btn_stop_replay)
        right.addLayout(row_play)

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
    def _on_canvas_right_click(self, x: int, y: int) -> None:
        menu = QMenu(self)
        act_tap = menu.addAction(f"タップ追加  ({x}, {y})")
        action = menu.exec(self.canvas.mapToGlobal(
            self.canvas.mapFromGlobal(self.cursor().pos())
        ))
        if action == act_tap:
            self._add_tap_step(x, y)

    def _add_tap_step(self, x: int, y: int) -> None:
        self.scene.steps.append(Step(type="tap", params={"x": x, "y": y, "duration_ms": 100}))
        self.scene.steps.append(Step(type="wait_fixed", params={"seconds": 1.0}))
        self._refresh_step_list(select_last=True)
        self._log(f"tap 追加: ({x},{y})")

    def _on_canvas_click(self, x: int, y: int) -> None:
        if self._btn_drag.isChecked():
            self._handle_drag_click(x, y)
            return
        row = self.step_list.currentRow()
        if 0 <= row < len(self.scene.steps) and self.scene.steps[row].type == "if_image":
            self._add_tap_to_if_branch(x, y, row)
        else:
            self._add_tap_step(x, y)

    def _add_tap_to_if_branch(self, x: int, y: int, row: int) -> None:
        step = self.scene.steps[row]
        menu = QMenu(self)
        act_then = menu.addAction(f"🟢 then に追加  ({x}, {y})")
        act_else = menu.addAction(f"🔴 else に追加  ({x}, {y})")
        action = menu.exec(QCursor.pos())
        if action == act_then:
            step.params.setdefault("then_steps", []).append(
                {"type": "tap", "params": {"x": x, "y": y, "duration_ms": 100}}
            )
            branch = "then"
        elif action == act_else:
            step.params.setdefault("else_steps", []).append(
                {"type": "tap", "params": {"x": x, "y": y, "duration_ms": 100}}
            )
            branch = "else"
        else:
            return
        self._refresh_step_list(select_idx=row)
        self._update_branch_markers(step)
        self._log(f"if_image {branch} タップ追加: ({x},{y})")

    def _update_branch_markers(self, step) -> None:
        then_taps = [
            (s["params"]["x"], s["params"]["y"])
            for s in (step.params.get("then_steps") or [])
            if s.get("type") == "tap"
        ]
        else_taps = [
            (s["params"]["x"], s["params"]["y"])
            for s in (step.params.get("else_steps") or [])
            if s.get("type") == "tap"
        ]
        self.canvas.set_branch_markers(then_taps, else_taps)

    def _toggle_drag_mode(self, checked: bool) -> None:
        self._drag_start = None
        self.canvas.set_drag_pins(None, None)
        if checked:
            if self.canvas.current_pixmap() is None:
                QMessageBox.information(self, "情報",
                    "先に『スナップ更新』でスナップショットを取得してください")
                self._btn_drag.setChecked(False)
                return
            self._btn_drag.setStyleSheet(
                "QPushButton { background:#e65100; color:white; font-weight:bold; }"
            )
            self._log("ドラッグモード: キャンバスで始点をクリックしてください")
        else:
            self._btn_drag.setStyleSheet("")
            self._log("ドラッグモード解除")

    def _handle_drag_click(self, x: int, y: int) -> None:
        if self._drag_start is None:
            self._drag_start = (x, y)
            self.canvas.set_drag_pins((x, y), None)
            self._log(f"ドラッグ始点: ({x},{y})  → 次に終点をクリック")
        else:
            x1, y1 = self._drag_start
            self._drag_start = None
            self.canvas.set_drag_pins((x1, y1), (x, y))
            self._btn_drag.setChecked(False)
            self._btn_drag.setStyleSheet("")
            self._add_drag_step(x1, y1, x, y)

    def _add_drag_step(self, x1: int, y1: int, x2: int, y2: int) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("ドラッグ設定")
        lay = QVBoxLayout(dlg)
        form = QFormLayout()
        lay.addLayout(form)
        form.addRow("始点 (x1, y1):", QLabel(f"{x1},  {y1}"))
        form.addRow("終点 (x2, y2):", QLabel(f"{x2},  {y2}"))
        spin = QDoubleSpinBox()
        spin.setRange(0.1, 10.0)
        spin.setDecimals(1)
        spin.setSingleStep(0.1)
        spin.setValue(0.5)
        spin.setSuffix(" 秒")
        form.addRow("ドラッグ時間:", spin)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        if dlg.exec() != QDialog.Accepted:
            self.canvas.set_drag_pins(None, None)
            return
        duration_ms = int(spin.value() * 1000)
        step = Step(type="swipe", params={
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "duration_ms": duration_ms,
        })
        self.scene.steps.append(step)
        self.scene.steps.append(Step(type="wait_fixed", params={"seconds": 1.0}))
        self.canvas.set_drag_pins(None, None)
        self._refresh_step_list(select_last=True)
        self._log(f"ドラッグ追加: ({x1},{y1})→({x2},{y2}) {spin.value()}s")

    def _toggle_reselect_mode(self, checked: bool) -> None:
        if checked:
            row = self.step_list.currentRow()
            if row < 0 or row >= len(self.scene.steps):
                self.btn_reselect.setChecked(False)
                return
            self._reselect_step_idx = row
            self._log("再設定モード: キャンバスで新しいマッチ範囲をドラッグしてください")
        else:
            self._cancel_reselect_mode()

    def _cancel_reselect_mode(self) -> None:
        self._reselect_step_idx = None
        if hasattr(self, "btn_reselect"):
            self.btn_reselect.setChecked(False)

    def _on_canvas_region(self, x: int, y: int, w: int, h: int) -> None:
        pm = self.canvas.current_pixmap()
        if pm is None:
            QMessageBox.information(self, "情報",
                                    "スナップショットがありません。先に『スナップ更新』を押してください")
            return

        # ---- テンプレート再設定モード ----
        if self._reselect_step_idx is not None:
            idx = self._reselect_step_idx
            self._cancel_reselect_mode()
            if idx >= len(self.scene.steps):
                return
            step = self.scene.steps[idx]
            os.makedirs(TEMPLATES_DIR, exist_ok=True)
            name = f"tpl_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
            path = os.path.join(TEMPLATES_DIR, name).replace("\\", "/")
            if not pm.copy(x, y, w, h).save(path):
                QMessageBox.warning(self, "エラー", "テンプレート保存失敗")
                return
            step.params["template"] = path
            step.params["region"] = [x, y, w, h]
            self._refresh_step_list(select_idx=idx)
            self._log(f"テンプレート再設定: [{idx + 1}] {step.type}  {path}")
            return

        # ---- 新規ステップ追加モード ----
        menu = QMenu(self)
        act_wait = menu.addAction("🕐 画像が出るまで待つ  (wait_image)")
        act_tap  = menu.addAction("👆 画像が出たらタップ  (tap_image)")
        act_if   = menu.addAction("🔀 マッチしたら分岐  (if_image)")
        action = menu.exec(self.canvas.mapToGlobal(
            self.canvas.mapFromGlobal(self.cursor().pos())
        ))
        if action not in (act_wait, act_tap, act_if):
            return

        os.makedirs(TEMPLATES_DIR, exist_ok=True)
        name = f"tpl_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
        path = os.path.join(TEMPLATES_DIR, name).replace("\\", "/")
        if not pm.copy(x, y, w, h).save(path):
            QMessageBox.warning(self, "エラー", "テンプレート保存失敗")
            return

        if action == act_tap:
            step = Step(type="tap_image", params={
                "template": path, "region": [x, y, w, h],
                "threshold": 0.85, "timeout_s": 30,
            })
            self.scene.steps.append(step)
            self._refresh_step_list(select_last=True)
            self._log(f"tap_image 追加: {path}")
        elif action == act_if:
            self._add_if_image_step(path, x, y, w, h)
        else:
            step = Step(type="wait_image", params={
                "template": path, "region": [x, y, w, h],
                "threshold": 0.85, "timeout_s": 30,
            })
            self.scene.steps.append(step)
            self._refresh_step_list(select_last=True)
            self._log(f"wait_image 追加: {path}")

    def _add_if_image_step(self, tpl_path: str, x: int, y: int, w: int, h: int) -> None:
        params: dict = {
            "template": tpl_path, "region": [x, y, w, h],
            "threshold": 0.85, "then_steps": [], "else_steps": [],
        }
        dlg = _IfImageBranchDialog(params, snapshot=self.canvas.current_pixmap(), parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        params["then_steps"] = dlg.get_then_steps()
        params["else_steps"] = dlg.get_else_steps()
        self.scene.steps.append(Step(type="if_image", params=params))
        self._refresh_step_list(select_last=True)
        self._log(
            f"if_image 追加: then={len(params['then_steps'])}ステップ"
            f"  else={len(params['else_steps'])}ステップ"
        )

    # ---------------------------------------------------------------- steps
    def _add_keyevent(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("キーイベント選択")
        lay = QVBoxLayout(dlg)
        combo = QComboBox()
        for label, code in _KEYEVENTS:
            combo.addItem(label, code)
        lay.addWidget(combo)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        if dlg.exec() != QDialog.Accepted:
            return
        keycode = combo.currentData()
        label = combo.currentText()
        step = Step(type="keyevent", params={"keycode": keycode})
        self.scene.steps.append(step)
        self._refresh_step_list(select_last=True)
        self._log(f"keyevent 追加: {label}")

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

    def _selected_rows(self) -> list[int]:
        n = len(self.scene.steps)
        return sorted(
            r for r in range(self.step_list.count())
            if self.step_list.item(r) and self.step_list.item(r).isSelected() and r < n
        )

    def _restore_selection(self, rows: list[int]) -> None:
        for r in rows:
            if 0 <= r < self.step_list.count():
                self.step_list.item(r).setSelected(True)
        if rows:
            self.step_list.setCurrentRow(rows[-1])

    def _del_step(self) -> None:
        rows = self._selected_rows()
        if not rows:
            row = self.step_list.currentRow()
            if row < 0 or row >= len(self.scene.steps):
                return
            rows = [row]
        for r in reversed(rows):
            self.scene.steps.pop(r)
        self._refresh_step_list()
        self._log(f"削除: {len(rows)} ステップ")

    def _on_steps_reordered(self, _parent, src, _end, _dst, dst) -> None:
        # QListWidget の InternalMove はビューを先に動かすので、scene.steps をそれに合わせる
        actual_dst = dst if dst > src else dst
        step = self.scene.steps.pop(src)
        self.scene.steps.insert(actual_dst, step)
        self._refresh_canvas_view()

    def _move_step_up(self) -> None:
        rows = self._selected_rows()
        if not rows or rows[0] <= 0:
            return
        steps = self.scene.steps
        if rows == list(range(rows[0], rows[-1] + 1)):
            # 連続ブロック: 直上の要素をブロック末尾の下へ
            above = steps.pop(rows[0] - 1)
            steps.insert(rows[-1], above)
            new_rows = [r - 1 for r in rows]
        else:
            # 非連続: 各行を独立して上へ
            for r in rows:
                if r > 0 and r - 1 not in rows:
                    steps[r - 1], steps[r] = steps[r], steps[r - 1]
            new_rows = [r - 1 if r > 0 and r - 1 not in rows else r for r in rows]
        self._refresh_step_list()
        self._restore_selection(new_rows)

    def _move_step_down(self) -> None:
        rows = self._selected_rows()
        n = len(self.scene.steps)
        if not rows or rows[-1] >= n - 1:
            return
        steps = self.scene.steps
        if rows == list(range(rows[0], rows[-1] + 1)):
            # 連続ブロック: 直下の要素をブロック先頭の上へ
            below = steps.pop(rows[-1] + 1)
            steps.insert(rows[0], below)
            new_rows = [r + 1 for r in rows]
        else:
            # 非連続: 各行を独立して下へ
            rows_set = set(rows)
            for r in reversed(rows):
                if r < n - 1 and r + 1 not in rows_set:
                    steps[r], steps[r + 1] = steps[r + 1], steps[r]
            new_rows = [r + 1 if r < n - 1 and r + 1 not in rows_set else r for r in rows]
        self._refresh_step_list()
        self._restore_selection(new_rows)

    def _import_scene(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "取り込むシーンを選択", SCENES_DIR, "JSON (*.json)"
        )
        if not path:
            return
        try:
            sub = load_scene(path)
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"読込失敗: {e}")
            return

        r = QMessageBox.question(
            self, "グループヘッダー",
            f"「{sub.name}」を取り込みます。\nグループヘッダーを追加しますか？",
            QMessageBox.Yes | QMessageBox.No,
        )

        steps_to_insert: list[Step] = []
        if r == QMessageBox.Yes:
            steps_to_insert.append(Step(type="group_header", params={"label": sub.name}))
        steps_to_insert.extend(sub.steps)

        # 選択行の次に挿入、未選択なら末尾
        row = self.step_list.currentRow()
        insert_at = (row + 1) if row >= 0 else len(self.scene.steps)
        for j, step in enumerate(steps_to_insert):
            self.scene.steps.insert(insert_at + j, step)

        self._refresh_step_list()
        self.step_list.setCurrentRow(insert_at + len(steps_to_insert) - 1)
        self._log(f"シーン取り込み: {sub.name}  {len(sub.steps)} ステップ")

    def _add_group_header(self) -> None:
        label, ok = QInputDialog.getText(self, "グループ名", "グループ名を入力:")
        if not ok or not label.strip():
            return
        row = self.step_list.currentRow()
        insert_at = (row + 1) if row >= 0 else len(self.scene.steps)
        step = Step(type="group_header", params={"label": label.strip()})
        self.scene.steps.insert(insert_at, step)
        self._refresh_step_list()
        self.step_list.setCurrentRow(insert_at)
        self._log(f"グループ追加: {label.strip()}")

    def _add_pick_scene(self) -> None:
        dlg = _PickSceneDialog({}, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        params = dlg.get_params()
        if not params["scenes"]:
            QMessageBox.information(self, "情報", "シーンを1つ以上追加してください")
            return
        row = self.step_list.currentRow()
        insert_at = (row + 1) if row >= 0 else len(self.scene.steps)
        step = Step(type="pick_scene", params=params)
        self.scene.steps.insert(insert_at, step)
        self._refresh_step_list(select_idx=insert_at)
        mode_lbl = "順番" if params["mode"] == "sequential" else "ランダム"
        self._log(f"pick_scene 追加: {mode_lbl} {len(params['scenes'])} 択")

    def _edit_pick_scene(self, row: int, step: Step) -> None:
        dlg = _PickSceneDialog(step.params, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        params = dlg.get_params()
        if not params["scenes"]:
            QMessageBox.information(self, "情報", "シーンを1つ以上追加してください")
            return
        step.params.update(params)
        self._refresh_step_list(select_idx=row)
        mode_lbl = "順番" if params["mode"] == "sequential" else "ランダム"
        self._log(f"pick_scene 更新: {mode_lbl} {len(params['scenes'])} 択")

    def _on_marker_moved(self, marker_idx: int, lx: int, ly: int) -> None:
        if marker_idx < 0 or marker_idx >= len(self._marker_step_indices):
            return
        step_idx = self._marker_step_indices[marker_idx]
        s = self.scene.steps[step_idx]
        old_x, old_y = s.params.get("x", 0), s.params.get("y", 0)
        s.params["x"] = lx
        s.params["y"] = ly
        self._refresh_step_list()
        self.step_list.setCurrentRow(step_idx)
        self._log(f"tap 移動: ({old_x},{old_y}) → ({lx},{ly})")

    def _refresh_step_list(self, select_last: bool = False, select_idx: int | None = None) -> None:
        self.step_list.blockSignals(True)
        self.step_list.clear()
        in_group = False
        for i, s in enumerate(self.scene.steps):
            if s.type == "group_header":
                in_group = True
                lbl = s.params.get("label", "")
                item = QListWidgetItem(f"┄┄ {lbl} ┄┄")
                f = QFont()
                f.setBold(True)
                item.setFont(f)
                item.setForeground(QBrush(QColor("#1565C0")))
                item.setBackground(QBrush(QColor("#E3F2FD")))
                self.step_list.addItem(item)
                continue

            pad = "    " if in_group else ""  # グループ内は4文字インデント

            if s.type == "tap":
                label = f"{pad}{i + 1}. 👆 タップ ({s.params.get('x')},{s.params.get('y')}) {s.params.get('duration_ms')}ms"
            elif s.type == "snapshot":
                label = f"{pad}{i + 1}. 📷 スナップ  {os.path.basename(s.params.get('path', ''))}"
            elif s.type == "wait_fixed":
                label = f"{pad}{i + 1}. ⏱ 待ち {s.params.get('seconds')}s"
            elif s.type == "wait_image":
                label = f"{pad}{i + 1}. 🕐 画像待ち  {os.path.basename(s.params.get('template', ''))}"
            elif s.type == "tap_image":
                label = f"{pad}{i + 1}. 👆 画像タップ  {os.path.basename(s.params.get('template', ''))}"
            elif s.type == "if_image":
                then_n = f"{len(s.params.get('then_steps') or [])}ステップ"
                else_n = f"{len(s.params.get('else_steps') or [])}ステップ"
                label = (f"{pad}{i + 1}. 🔀 画像分岐  {os.path.basename(s.params.get('template', ''))}"
                         f"  ✓→{then_n}  ✗→{else_n}")
            elif s.type == "swipe":
                label = (f"{pad}{i + 1}. ↔ スワイプ ({s.params.get('x1')},{s.params.get('y1')})"
                         f"→({s.params.get('x2')},{s.params.get('y2')}) "
                         f"{s.params.get('duration_ms')}ms")
            elif s.type == "scroll":
                p = s.params
                label = (f"{pad}{i + 1}. ↕ スクロール "
                         f"({p.get('x1')}±{p.get('x1_jitter',0)},"
                         f"{p.get('y1')}±{p.get('y1_jitter',0)})"
                         f"→({p.get('x2')}±{p.get('x2_jitter',0)},"
                         f"{p.get('y2')}±{p.get('y2_jitter',0)}) "
                         f"{p.get('duration_ms')}±{p.get('duration_jitter_ms',0)}ms")
            elif s.type == "keyevent":
                kc = s.params.get("keycode", "")
                disp = next((l for l, c in _KEYEVENTS if c == kc), kc)
                label = f"{pad}{i + 1}. 🔑 {disp}"
            elif s.type == "call_scene":
                sub = s.params.get("scene", "")
                name = os.path.splitext(os.path.basename(sub))[0] if sub else "(未設定)"
                label = f"{pad}{i + 1}. 📂 シーン呼出  {name}"
            elif s.type == "pick_scene":
                mode = s.params.get("mode", "random")
                mode_icon = "🔄" if mode == "sequential" else "🎲"
                mode_lbl  = "順番" if mode == "sequential" else "ランダム"
                cnt = len(s.params.get("scenes") or [])
                names = "、".join(
                    os.path.splitext(os.path.basename(sc))[0]
                    for sc in (s.params.get("scenes") or [])
                )
                label = f"{pad}{i + 1}. {mode_icon} {mode_lbl}選択 {cnt}択  [{names}]"
            else:
                label = f"{pad}{i + 1}. {s.type}  {s.params}"
            self.step_list.addItem(label)
        if select_idx is not None:
            self.step_list.setCurrentRow(select_idx)
        elif select_last and self.scene.steps:
            self.step_list.setCurrentRow(len(self.scene.steps) - 1)
        self.step_list.blockSignals(False)
        self._refresh_canvas_view()

    def _on_step_row_changed(self, row: int) -> None:
        self._refresh_canvas_view()
        step_type = self.scene.steps[row].type if 0 <= row < len(self.scene.steps) else ""
        is_image_step = step_type in ("wait_image", "tap_image")
        is_reselectable = step_type in ("wait_image", "tap_image", "if_image")
        self.btn_match_test.setEnabled(is_image_step)
        self.btn_reselect.setEnabled(is_reselectable)
        if not is_reselectable:
            self._cancel_reselect_mode()

        if is_reselectable and 0 <= row < len(self.scene.steps):
            step = self.scene.steps[row]
            self.canvas.set_match_overlay(step.params.get("region"), None, None)
            if step_type == "if_image":
                self._update_branch_markers(step)
            else:
                self.canvas.clear_branch_markers()
        else:
            self.canvas.clear_match_overlay()
            self.canvas.clear_branch_markers()

    def _on_step_double_clicked(self, item) -> None:
        row = self.step_list.row(item)
        if row < 0 or row >= len(self.scene.steps):
            return
        step = self.scene.steps[row]
        if step.type == "wait_fixed":
            self._edit_wait_fixed(row, step)
        elif step.type == "if_image":
            self._edit_if_image(row, step)
        elif step.type == "pick_scene":
            self._edit_pick_scene(row, step)

    def _edit_if_image(self, row: int, step) -> None:
        dlg = _IfImageBranchDialog(step.params, snapshot=self.canvas.current_pixmap(), parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        step.params["then_steps"] = dlg.get_then_steps()
        step.params["else_steps"] = dlg.get_else_steps()
        self._refresh_step_list(select_idx=row)
        self._update_branch_markers(step)
        self._log(
            f"if_image 更新: then={len(step.params['then_steps'])}ステップ"
            f" else={len(step.params['else_steps'])}ステップ"
        )

    def _edit_wait_fixed(self, row: int, step) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("固定待ち — 秒数を編集")
        lay = QVBoxLayout(dlg)
        form = QFormLayout()
        spin = QDoubleSpinBox()
        spin.setRange(0.1, 3600.0)
        spin.setDecimals(1)
        spin.setSingleStep(0.5)
        spin.setSuffix(" 秒")
        spin.setValue(float(step.params.get("seconds", 1.0)))
        form.addRow("待ち時間:", spin)
        lay.addLayout(form)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        if dlg.exec() == QDialog.Accepted:
            step.params["seconds"] = round(spin.value(), 1)
            self._refresh_step_list(select_idx=row)
            self._log(f"wait_fixed 更新: {step.params['seconds']}s")

    # ----------------------------------------------------------- canvas view
    def _compute_view(
        self, selected_idx: int | None
    ) -> tuple[str | None, list[tuple[int, int, int, bool]], list[int]]:
        snapshots: list[str] = []
        group: list[int] = []
        current = -1
        for s in self.scene.steps:
            if s.type == "snapshot":
                snapshots.append(s.params.get("path", ""))
                current = len(snapshots) - 1
            group.append(current)

        if not snapshots:
            return None, [], []

        if selected_idx is not None and 0 <= selected_idx < len(self.scene.steps):
            display_group = group[selected_idx]
            highlight_idx = selected_idx
        else:
            display_group = len(snapshots) - 1
            highlight_idx = None

        if display_group < 0:
            return None, [], []

        markers: list[tuple[int, int, int, bool]] = []
        marker_step_indices: list[int] = []
        n = 0
        for i, s in enumerate(self.scene.steps):
            if group[i] != display_group or s.type != "tap":
                continue
            n += 1
            hi = (i == highlight_idx)
            markers.append((n, int(s.params.get("x", 0)), int(s.params.get("y", 0)), hi))
            marker_step_indices.append(i)

        return snapshots[display_group], markers, marker_step_indices

    def _refresh_canvas_view(self) -> None:
        row = self.step_list.currentRow()
        sel = row if row >= 0 else None
        path, markers, self._marker_step_indices = self._compute_view(sel)
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
    def _toggle_recording(self) -> None:
        if self.recorder and self.recorder.is_running():
            self._stop_recording()
        else:
            self._start_recording()

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

        self.btn_rec_toggle.setText("■ 記録停止")
        self.btn_rec_toggle.setStyleSheet(
            "QPushButton { background-color: #c62828; color: white; font-weight: bold; }"
        )
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
        self.btn_rec_toggle.setText("● 記録開始")
        self.btn_rec_toggle.setStyleSheet("")
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

    # --------------------------------------------------------- match test
    def _test_match_step(self) -> None:
        row = self.step_list.currentRow()
        if row < 0 or row >= len(self.scene.steps):
            return
        step = self.scene.steps[row]
        if step.type not in ("wait_image", "tap_image"):
            return
        serial = self._require_connected()
        if not serial:
            return

        self.btn_match_test.setEnabled(False)
        self.btn_match_test.setText("🔍 テスト中…")
        self._log("マッチテスト開始…")

        p = step.params
        template_path = p.get("template", "")
        region = p.get("region")
        threshold = float(p.get("threshold", 0.85))

        def run():
            import cv2, numpy as np
            result: dict = {"error": None, "score": None, "match_rect": None,
                            "region": region, "threshold": threshold, "img_arr": None}
            try:
                png = screencap(serial)
                arr = np.frombuffer(png, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is None:
                    result["error"] = "スクショのデコード失敗"
                    self.match_result_signal.emit(result)
                    return
                result["img_arr"] = img

                tmpl = cv2.imread(template_path, cv2.IMREAD_COLOR)
                if tmpl is None:
                    result["error"] = f"テンプレート読込失敗: {template_path}"
                    self.match_result_signal.emit(result)
                    return

                target = img
                ox, oy = 0, 0
                if region and len(region) == 4:
                    rx, ry, rw, rh = region
                    h_img, w_img = img.shape[:2]
                    x2, y2 = min(rx + rw, w_img), min(ry + rh, h_img)
                    rx, ry = max(0, rx), max(0, ry)
                    target = img[ry:y2, rx:x2]
                    ox, oy = rx, ry

                th, tw = tmpl.shape[:2]
                if target.shape[0] < th or target.shape[1] < tw:
                    result["error"] = "検索範囲がテンプレートより小さい"
                    self.match_result_signal.emit(result)
                    return

                res = cv2.matchTemplate(target, tmpl, cv2.TM_CCOEFF_NORMED)
                _, maxv, _, maxloc = cv2.minMaxLoc(res)
                result["score"] = float(maxv)
                result["match_rect"] = [ox + maxloc[0], oy + maxloc[1], tw, th]
            except Exception as e:
                result["error"] = str(e)
            self.match_result_signal.emit(result)

        import threading
        threading.Thread(target=run, daemon=True).start()

    def _on_match_result(self, result: dict) -> None:
        import cv2, numpy as np
        self.btn_match_test.setEnabled(True)
        self.btn_match_test.setText("🔍 マッチテスト")

        if result.get("error"):
            self._log(f"マッチテスト失敗: {result['error']}")
            return

        score = result["score"]
        threshold = result["threshold"]
        ok = score >= threshold
        mark = "✓" if ok else "✗"
        self._log(f"マッチテスト結果: {mark} score={score:.4f}  threshold={threshold}  "
                  f"{'一致' if ok else '不一致'}")

        # スクショをキャンバスに表示
        img_arr = result.get("img_arr")
        if img_arr is not None:
            rgb = cv2.cvtColor(img_arr, cv2.COLOR_BGR2RGB)
            from PySide6.QtGui import QImage
            h, w = rgb.shape[:2]
            qimg = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888)
            pm = QPixmap.fromImage(qimg)
            self.canvas.set_snapshot(pm)

        self.canvas.set_match_overlay(
            result.get("region"),
            result.get("match_rect"),
            score,
            threshold,
        )

    # --------------------------------------------------------- single step run
    def _run_selected_step(self) -> None:
        row = self.step_list.currentRow()
        if row < 0 or row >= len(self.scene.steps):
            QMessageBox.information(self, "情報", "実行するステップを選択してください")
            return
        step = self.scene.steps[row]
        if step.type in ("snapshot", "group_header"):
            QMessageBox.information(self, "情報", f"「{step.type}」は実行できません")
            return
        serial = self._require_connected()
        if not serial:
            return
        if self.replay_thread and self.replay_thread.is_alive():
            QMessageBox.information(self, "情報", "再生中です。停止してから実行してください")
            return

        tmp_scene = Scene()
        tmp_scene.steps = [step]
        self.replay_stop.clear()
        self.btn_run_step.setEnabled(False)
        self.btn_replay.setEnabled(False)
        self.btn_stop_replay.setEnabled(True)
        self._log(f"1行実行: [{row + 1}] {step.type} {step.params}")

        _row = row
        self.step_highlight_signal.emit(_row)

        def run():
            try:
                replay_scene(
                    tmp_scene, serial,
                    log=self._log,
                    should_stop=self.replay_stop.is_set,
                )
            except Exception as e:
                self._log(f"エラー: {e}")
            finally:
                self.replay_finished.emit()

        self.replay_thread = threading.Thread(target=run, daemon=True)
        self.replay_thread.start()

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
                    on_step=self.step_highlight_signal.emit,
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

    def _on_step_highlight(self, idx: int) -> None:
        if self._highlighted_row is not None:
            item = self.step_list.item(self._highlighted_row)
            if item:
                item.setBackground(QBrush())
        item = self.step_list.item(idx)
        if item:
            item.setBackground(QBrush(QColor("#FFF8E1")))
            self.step_list.scrollToItem(item)
        self._highlighted_row = idx

    def _clear_step_highlight(self) -> None:
        if self._highlighted_row is not None:
            item = self.step_list.item(self._highlighted_row)
            if item:
                item.setBackground(QBrush())
            self._highlighted_row = None

    def _on_replay_finished(self) -> None:
        self._clear_step_highlight()
        self.btn_replay.setEnabled(True)
        self.btn_run_step.setEnabled(True)
        self.btn_stop_replay.setEnabled(False)

    # ------------------------------------------------------------ shutdown
    def shutdown(self) -> None:
        """MainWindow の closeEvent から呼ぶ。"""
        self.replay_stop.set()
        if self.recorder:
            self.recorder.stop()
