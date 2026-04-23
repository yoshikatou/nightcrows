"""ウォッチャー編集タブ。

フロー JSON の watchers リストを GUI で編集する。
条件種別ごとにフォームを切り替え、ハンドラシーン・発火後動作を設定できる。
"""
from __future__ import annotations

import os
import uuid

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QBrush, QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox,
    QStackedWidget, QVBoxLayout, QWidget,
)

from .flow import Condition, Flow, Watcher, load_flow, save_flow

FLOWS_DIR = "flows"
SCENES_DIR = "scenes"
TEMPLATES_DIR = "templates"

_COND_LABELS = {
    "image_appear": "画像が出現したとき",
    "image_gone":   "画像が消えたとき",
    "digit_threshold": "数字が閾値を超えたとき（ポーション残量など）",
}

_AFTER_LABELS = {
    "restart_scene": "現在のシーンを最初からやり直す",
    "next_scene":    "次のシーンへ進む",
    "stop":          "フローを停止する",
}


# ---------------------------------------------------------------- 条件フォーム
class _ImageConditionForm(QWidget):
    """image_appear / image_gone 共通フォーム。"""

    def __init__(self, show_consecutive: bool = False) -> None:
        super().__init__()
        self._show_cons = show_consecutive
        lay = QFormLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        # テンプレ画像
        h = QHBoxLayout()
        self.tmpl_edit = QLineEdit()
        self.tmpl_edit.setPlaceholderText("templates/ 以下の画像ファイル")
        btn = QPushButton("参照")
        btn.setFixedWidth(50)
        btn.clicked.connect(self._browse)
        h.addWidget(self.tmpl_edit, 1)
        h.addWidget(btn)
        lay.addRow("テンプレ画像:", h)

        # Region
        rh = QHBoxLayout()
        self.rx = QSpinBox(); self.rx.setRange(0, 9999); self.rx.setPrefix("x:")
        self.ry = QSpinBox(); self.ry.setRange(0, 9999); self.ry.setPrefix("y:")
        self.rw = QSpinBox(); self.rw.setRange(0, 9999); self.rw.setPrefix("w:")
        self.rh_spin = QSpinBox(); self.rh_spin.setRange(0, 9999); self.rh_spin.setPrefix("h:")
        for sp in (self.rx, self.ry, self.rw, self.rh_spin):
            rh.addWidget(sp)
        self.region_check = QCheckBox("領域指定")
        self.region_check.stateChanged.connect(self._on_region_toggle)
        lay.addRow(self.region_check, rh)
        self._on_region_toggle(Qt.Unchecked)

        # 閾値
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.0, 1.0)
        self.threshold.setSingleStep(0.01)
        self.threshold.setValue(0.85)
        self.threshold.setDecimals(2)
        lay.addRow("マッチ閾値:", self.threshold)

        # consecutive (image_gone のみ)
        self.cons_label = QLabel("連続ミス回数:")
        self.cons_spin = QSpinBox()
        self.cons_spin.setRange(1, 30)
        self.cons_spin.setValue(3)
        if not show_consecutive:
            self.cons_label.hide()
            self.cons_spin.hide()
        lay.addRow(self.cons_label, self.cons_spin)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "テンプレ画像選択", TEMPLATES_DIR,
            "画像 (*.png *.jpg *.bmp)"
        )
        if path:
            self.tmpl_edit.setText(path)

    def _on_region_toggle(self, state) -> None:
        on = bool(state)
        for sp in (self.rx, self.ry, self.rw, self.rh_spin):
            sp.setEnabled(on)

    def load(self, c: Condition) -> None:
        self.tmpl_edit.setText(c.template)
        self.threshold.setValue(c.threshold)
        if c.region and len(c.region) == 4:
            self.region_check.setChecked(True)
            self.rx.setValue(c.region[0])
            self.ry.setValue(c.region[1])
            self.rw.setValue(c.region[2])
            self.rh_spin.setValue(c.region[3])
        else:
            self.region_check.setChecked(False)
        self.cons_spin.setValue(c.consecutive)

    def to_condition(self, ctype: str) -> Condition:
        region = []
        if self.region_check.isChecked():
            region = [self.rx.value(), self.ry.value(),
                      self.rw.value(), self.rh_spin.value()]
        return Condition(
            type=ctype,
            template=self.tmpl_edit.text().strip(),
            region=region,
            threshold=self.threshold.value(),
            consecutive=self.cons_spin.value(),
        )


class _DigitConditionForm(QWidget):
    """digit_threshold フォーム。"""

    def __init__(self) -> None:
        super().__init__()
        lay = QFormLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        # digits_dir
        h = QHBoxLayout()
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText("0.png〜9.png が入ったフォルダ")
        btn = QPushButton("参照")
        btn.setFixedWidth(50)
        btn.clicked.connect(self._browse)
        h.addWidget(self.dir_edit, 1)
        h.addWidget(btn)
        lay.addRow("桁テンプレフォルダ:", h)

        # Region
        rh = QHBoxLayout()
        self.rx = QSpinBox(); self.rx.setRange(0, 9999); self.rx.setPrefix("x:")
        self.ry = QSpinBox(); self.ry.setRange(0, 9999); self.ry.setPrefix("y:")
        self.rw = QSpinBox(); self.rw.setRange(0, 9999); self.rw.setPrefix("w:")
        self.rh_spin = QSpinBox(); self.rh_spin.setRange(0, 9999); self.rh_spin.setPrefix("h:")
        for sp in (self.rx, self.ry, self.rw, self.rh_spin):
            rh.addWidget(sp)
        self.region_check = QCheckBox("領域指定")
        self.region_check.stateChanged.connect(self._on_region_toggle)
        lay.addRow(self.region_check, rh)
        self._on_region_toggle(Qt.Unchecked)

        # op / value
        hv = QHBoxLayout()
        self.op_combo = QComboBox()
        for op in ("<", "<=", ">", ">=", "=="):
            self.op_combo.addItem(op, op)
        hv.addWidget(self.op_combo)
        self.value_spin = QSpinBox()
        self.value_spin.setRange(0, 99999)
        hv.addWidget(self.value_spin)
        hv.addStretch()
        lay.addRow("条件（数値）:", hv)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "桁テンプレフォルダ選択", "")
        if d:
            self.dir_edit.setText(d)

    def _on_region_toggle(self, state) -> None:
        on = bool(state)
        for sp in (self.rx, self.ry, self.rw, self.rh_spin):
            sp.setEnabled(on)

    def load(self, c: Condition) -> None:
        self.dir_edit.setText(c.digits_dir)
        if c.region and len(c.region) == 4:
            self.region_check.setChecked(True)
            self.rx.setValue(c.region[0])
            self.ry.setValue(c.region[1])
            self.rw.setValue(c.region[2])
            self.rh_spin.setValue(c.region[3])
        else:
            self.region_check.setChecked(False)
        idx = self.op_combo.findData(c.op)
        if idx >= 0:
            self.op_combo.setCurrentIndex(idx)
        self.value_spin.setValue(c.value)

    def to_condition(self) -> Condition:
        region = []
        if self.region_check.isChecked():
            region = [self.rx.value(), self.ry.value(),
                      self.rw.value(), self.rh_spin.value()]
        return Condition(
            type="digit_threshold",
            digits_dir=self.dir_edit.text().strip(),
            region=region,
            op=self.op_combo.currentData(),
            value=self.value_spin.value(),
        )


# -------------------------------------------------------------- 編集ダイアログ
class _WatcherDialog(QDialog):
    """ウォッチャー 1件の追加/編集ダイアログ。"""

    def __init__(self, watcher: Watcher | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ウォッチャー設定")
        self.setMinimumSize(520, 580)

        lay = QVBoxLayout(self)

        form = QFormLayout()

        # ID / 有効
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("自動生成（空白で OK）")
        form.addRow("ID:", self.id_edit)

        self.enabled_check = QCheckBox("有効")
        self.enabled_check.setChecked(True)
        form.addRow("", self.enabled_check)

        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(0, 999)
        form.addRow("優先度:", self.priority_spin)

        lay.addLayout(form)

        # 条件種別
        grp_cond = QGroupBox("検知条件")
        cond_lay = QVBoxLayout(grp_cond)

        self.cond_combo = QComboBox()
        for key, label in _COND_LABELS.items():
            self.cond_combo.addItem(label, key)
        self.cond_combo.currentIndexChanged.connect(self._on_cond_changed)
        cond_lay.addWidget(self.cond_combo)

        self.stack = QStackedWidget()
        self.form_appear = _ImageConditionForm(show_consecutive=False)
        self.form_gone   = _ImageConditionForm(show_consecutive=True)
        self.form_digit  = _DigitConditionForm()
        self.stack.addWidget(self.form_appear)   # index 0
        self.stack.addWidget(self.form_gone)     # index 1
        self.stack.addWidget(self.form_digit)    # index 2
        cond_lay.addWidget(self.stack)
        lay.addWidget(grp_cond)

        # ハンドラ
        grp_handler = QGroupBox("発火時の動作")
        h_lay = QFormLayout(grp_handler)

        hh = QHBoxLayout()
        self.handler_edit = QLineEdit()
        self.handler_edit.setPlaceholderText("scenes/ 以下の .json（省略可）")
        btn_h = QPushButton("参照")
        btn_h.setFixedWidth(50)
        btn_h.clicked.connect(self._browse_handler)
        hh.addWidget(self.handler_edit, 1)
        hh.addWidget(btn_h)
        h_lay.addRow("実行シーン:", hh)

        self.after_combo = QComboBox()
        for key, label in _AFTER_LABELS.items():
            self.after_combo.addItem(label, key)
        h_lay.addRow("シーン完了後:", self.after_combo)

        self.cooldown_spin = QDoubleSpinBox()
        self.cooldown_spin.setRange(0, 3600)
        self.cooldown_spin.setSingleStep(1.0)
        self.cooldown_spin.setSuffix(" 秒")
        h_lay.addRow("クールダウン:", self.cooldown_spin)

        lay.addWidget(grp_handler)

        # ボタン
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        if watcher:
            self._load(watcher)

    def _on_cond_changed(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)

    def _browse_handler(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "ハンドラシーン選択", SCENES_DIR, "JSON (*.json)"
        )
        if path:
            rel = os.path.relpath(path, SCENES_DIR).replace("\\", "/")
            self.handler_edit.setText(rel)

    def _load(self, w: Watcher) -> None:
        self.id_edit.setText(w.id)
        self.enabled_check.setChecked(w.enabled)
        self.priority_spin.setValue(w.priority)

        ctype = w.condition.type
        idx = self.cond_combo.findData(ctype)
        if idx >= 0:
            self.cond_combo.setCurrentIndex(idx)
        if ctype == "image_appear":
            self.form_appear.load(w.condition)
        elif ctype == "image_gone":
            self.form_gone.load(w.condition)
        elif ctype == "digit_threshold":
            self.form_digit.load(w.condition)

        self.handler_edit.setText(w.handler)
        idx2 = self.after_combo.findData(w.after)
        if idx2 >= 0:
            self.after_combo.setCurrentIndex(idx2)
        self.cooldown_spin.setValue(w.cooldown_s)

    def _on_ok(self) -> None:
        self.accept()

    def result_watcher(self) -> Watcher:
        wid = self.id_edit.text().strip() or str(uuid.uuid4())[:8]
        ctype = self.cond_combo.currentData()
        if ctype == "image_appear":
            cond = self.form_appear.to_condition("image_appear")
        elif ctype == "image_gone":
            cond = self.form_gone.to_condition("image_gone")
        else:
            cond = self.form_digit.to_condition()

        return Watcher(
            id=wid,
            enabled=self.enabled_check.isChecked(),
            priority=self.priority_spin.value(),
            condition=cond,
            handler=self.handler_edit.text().strip(),
            after=self.after_combo.currentData(),
            cooldown_s=self.cooldown_spin.value(),
            interrupt="step_end",
        )


# --------------------------------------------------------------- メインウィジェット
class WatcherEditorWidget(QWidget):
    """ウォッチャー一覧と編集を提供するタブウィジェット。"""

    def __init__(self, main_window) -> None:
        super().__init__()
        self._mw = main_window
        self._flow: Flow | None = None
        self._flow_path: str | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        # フロー選択バー
        row = QHBoxLayout()
        row.addWidget(QLabel("フロー:"))
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("flows/ 以下の .json を選択")
        row.addWidget(self.path_edit, 1)
        btn_open = QPushButton("開く")
        btn_open.clicked.connect(self._open)
        self.btn_save = QPushButton("保存")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save)
        row.addWidget(btn_open)
        row.addWidget(self.btn_save)
        lay.addLayout(row)

        # ヒント
        hint = QLabel(
            "ウォッチャーは再生中、常時スクリーンショットを監視します。"
            "条件が満たされるとハンドラシーンを実行します。"
        )
        hint.setStyleSheet("color: #555; font-size: 10px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        # リスト
        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        lay.addWidget(self.list, 1)

        # 操作ボタン
        btn_row = QHBoxLayout()
        self.btn_add  = QPushButton("＋ 追加")
        self.btn_edit = QPushButton("✎ 編集")
        self.btn_del  = QPushButton("✕ 削除")
        self.btn_up   = QPushButton("↑")
        self.btn_down = QPushButton("↓")
        self.btn_toggle = QPushButton("有効/無効")
        for b in (self.btn_add, self.btn_edit, self.btn_del,
                  self.btn_up, self.btn_down, self.btn_toggle):
            btn_row.addWidget(b)
            b.setEnabled(False)
        self.btn_add.setEnabled(True)
        lay.addLayout(btn_row)

        self.btn_add.clicked.connect(self._add)
        self.btn_edit.clicked.connect(self._edit)
        self.btn_del.clicked.connect(self._delete)
        self.btn_up.clicked.connect(self._move_up)
        self.btn_down.clicked.connect(self._move_down)
        self.btn_toggle.clicked.connect(self._toggle_enabled)
        self.list.currentRowChanged.connect(self._on_selection_changed)
        self.list.itemDoubleClicked.connect(lambda _: self._edit())

    # --------------------------------------------------------- ファイル操作
    def _open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "フロー選択", FLOWS_DIR, "JSON (*.json)"
        )
        if not path:
            return
        try:
            flow = load_flow(path)
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"読込失敗: {e}")
            return
        self._flow = flow
        self._flow_path = path
        self.path_edit.setText(path)
        self.btn_save.setEnabled(True)
        self.btn_add.setEnabled(True)
        self._refresh_list()

    def _save(self) -> None:
        if not self._flow or not self._flow_path:
            return
        try:
            save_flow(self._flow, self._flow_path)
            QMessageBox.information(self, "保存完了", "フローを保存しました")
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"保存失敗: {e}")

    # --------------------------------------------------------- リスト更新
    def _refresh_list(self) -> None:
        self.list.clear()
        if not self._flow:
            return
        for w in self._flow.watchers:
            self.list.addItem(self._make_item(w))
        self._on_selection_changed(self.list.currentRow())

    def _make_item(self, w: Watcher) -> QListWidgetItem:
        ctype = w.condition.type
        cond_label = _COND_LABELS.get(ctype, ctype)
        after_label = _AFTER_LABELS.get(w.after, w.after)
        handler_name = (
            os.path.basename(w.handler).removesuffix(".json") if w.handler else "（なし）"
        )
        text = (
            f"[{'✓' if w.enabled else '✗'}]  {w.id}  |  {cond_label}"
            f"\n      → {handler_name}  /  {after_label}"
            f"  /  優先度:{w.priority}  冷却:{w.cooldown_s:.0f}s"
        )
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, w.id)
        if not w.enabled:
            item.setForeground(QBrush(QColor("#aaa")))
        else:
            item.setForeground(QBrush(QColor("#111")))
        font = QFont()
        font.setPointSize(9)
        item.setFont(font)
        return item

    def _on_selection_changed(self, row: int) -> None:
        has = row >= 0 and self._flow is not None
        for b in (self.btn_edit, self.btn_del,
                  self.btn_up, self.btn_down, self.btn_toggle):
            b.setEnabled(has)

    # --------------------------------------------------------- CRUD
    def _add(self) -> None:
        if self._flow is None:
            QMessageBox.information(self, "情報", "先にフローを開いてください")
            return
        dlg = _WatcherDialog(parent=self)
        if dlg.exec() == QDialog.Accepted:
            w = dlg.result_watcher()
            self._flow.watchers.append(w)
            self.list.addItem(self._make_item(w))

    def _edit(self) -> None:
        row = self.list.currentRow()
        if row < 0 or not self._flow:
            return
        w = self._flow.watchers[row]
        dlg = _WatcherDialog(watcher=w, parent=self)
        if dlg.exec() == QDialog.Accepted:
            new_w = dlg.result_watcher()
            self._flow.watchers[row] = new_w
            self.list.takeItem(row)
            self.list.insertItem(row, self._make_item(new_w))
            self.list.setCurrentRow(row)

    def _delete(self) -> None:
        row = self.list.currentRow()
        if row < 0 or not self._flow:
            return
        w = self._flow.watchers[row]
        if QMessageBox.question(
            self, "削除確認", f"ウォッチャー「{w.id}」を削除しますか？",
            QMessageBox.Yes | QMessageBox.No
        ) == QMessageBox.Yes:
            self._flow.watchers.pop(row)
            self.list.takeItem(row)

    def _move_up(self) -> None:
        row = self.list.currentRow()
        if row <= 0 or not self._flow:
            return
        watchers = self._flow.watchers
        watchers[row - 1], watchers[row] = watchers[row], watchers[row - 1]
        self._refresh_list()
        self.list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self.list.currentRow()
        if not self._flow or row < 0 or row >= len(self._flow.watchers) - 1:
            return
        watchers = self._flow.watchers
        watchers[row], watchers[row + 1] = watchers[row + 1], watchers[row]
        self._refresh_list()
        self.list.setCurrentRow(row + 1)

    def _toggle_enabled(self) -> None:
        row = self.list.currentRow()
        if row < 0 or not self._flow:
            return
        w = self._flow.watchers[row]
        w.enabled = not w.enabled
        self.list.takeItem(row)
        self.list.insertItem(row, self._make_item(w))
        self.list.setCurrentRow(row)
