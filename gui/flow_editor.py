"""フロー編集タブ — TV番組表スタイルの週間スケジュールエディタ。

列 = 曜日（月〜日）、行 = 時刻（30分刻み）。
セルをクリックしてシーンを割り当て、右クリックでクリア。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .flow import Flow, ScheduleEntry, load_flow, save_flow

FLOWS_DIR = "flows"
SCENES_DIR = "scenes"

DAYS = ["月", "火", "水", "木", "金", "土", "日"]

_SLOT_MIN = 30  # 分刻み


def _make_slots() -> list[str]:
    slots = []
    for h in range(24):
        for m in range(0, 60, _SLOT_MIN):
            slots.append(f"{h:02d}:{m:02d}")
    return slots


TIME_SLOTS = _make_slots()  # ["00:00", "00:30", ..., "23:30"]

_PALETTE = [
    "#BBDEFB", "#C8E6C9", "#FFE0B2", "#F8BBD0",
    "#E1BEE7", "#B2DFDB", "#FFF9C4", "#D7CCC8",
    "#B3E5FC", "#DCEDC8", "#FFCDD2", "#CFD8DC",
]


def _cell_color(scene_path: str) -> QColor:
    return QColor(_PALETTE[abs(hash(scene_path)) % len(_PALETTE)])


# ------------------------------------------------------------------ ダイアログ
class _ScenePickerDialog(QDialog):
    """scenes/ 以下の JSON を一覧して選択するダイアログ。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("シーン選択")
        self.setMinimumSize(380, 420)
        self._selected: str | None = None

        lay = QVBoxLayout(self)

        self.search = QLineEdit()
        self.search.setPlaceholderText("絞り込み…")
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._on_ok)
        lay.addWidget(self.list, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._all: list[str] = []
        self._load()

    def _load(self) -> None:
        self._all = []
        if os.path.isdir(SCENES_DIR):
            for root, _, files in os.walk(SCENES_DIR):
                for f in sorted(files):
                    if f.endswith(".json"):
                        rel = os.path.relpath(
                            os.path.join(root, f), SCENES_DIR
                        ).replace("\\", "/")
                        self._all.append(rel)
        self._filter("")

    def _filter(self, text: str) -> None:
        self.list.clear()
        for s in self._all:
            if text.lower() in s.lower():
                self.list.addItem(s)
        if self.list.count() > 0:
            self.list.setCurrentRow(0)

    def _on_ok(self) -> None:
        item = self.list.currentItem()
        if item:
            self._selected = item.text()
            self.accept()

    def selected(self) -> str | None:
        return self._selected


# ------------------------------------------------------------------ メイン
class FlowEditorWidget(QWidget):
    """週間スケジュールグリッドを持つフロー編集タブ。"""

    def __init__(self, main_window) -> None:
        super().__init__()
        self._mw = main_window
        self._flow: Flow | None = None
        self._flow_path: str | None = None
        self._build_ui()

    # ----------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        # フロー選択バー
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("フロー:"))
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("flows/ 以下の .json を選択")
        row1.addWidget(self.path_edit, 1)
        btn_open = QPushButton("開く")
        btn_open.clicked.connect(self._open)
        btn_new = QPushButton("新規")
        btn_new.clicked.connect(self._new)
        self.btn_save = QPushButton("保存")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save)
        row1.addWidget(btn_open)
        row1.addWidget(btn_new)
        row1.addWidget(self.btn_save)
        lay.addLayout(row1)

        # フロー名
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("フロー名:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("untitled")
        row2.addWidget(self.name_edit, 1)
        lay.addLayout(row2)

        # ヒント
        hint = QLabel(
            "左クリック：シーン割り当て　右クリック：クリア　"
            "※daily / once エントリは JSON で管理、このグリッドは weekly のみ"
        )
        hint.setStyleSheet("color: #666; font-size: 10px;")
        lay.addWidget(hint)

        # グリッド
        self.table = QTableWidget(len(TIME_SLOTS), 7)
        self.table.setHorizontalHeaderLabels(DAYS)
        self.table.setVerticalHeaderLabels(TIME_SLOTS)

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)

        vh = self.table.verticalHeader()
        vh.setDefaultSectionSize(26)
        vh.setSectionResizeMode(QHeaderView.Fixed)

        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)

        lay.addWidget(self.table, 1)

    # -------------------------------------------------------------- ファイル操作
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
        self.name_edit.setText(flow.name)
        self._populate_grid()
        self.btn_save.setEnabled(True)

    def _new(self) -> None:
        os.makedirs(FLOWS_DIR, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self, "新規フロー保存先", FLOWS_DIR, "JSON (*.json)"
        )
        if not path:
            return
        name = os.path.splitext(os.path.basename(path))[0]
        self._flow = Flow(name=name)
        self._flow_path = path
        self.path_edit.setText(path)
        self.name_edit.setText(name)
        self._clear_grid()
        self.btn_save.setEnabled(True)

    def _save(self) -> None:
        if not self._flow or not self._flow_path:
            return
        self._flow.name = self.name_edit.text().strip() or "untitled"
        weekly = self._grid_to_schedule()
        other = [e for e in self._flow.schedule if e.repeat != "weekly"]
        self._flow.schedule = other + weekly
        try:
            save_flow(self._flow, self._flow_path)
            QMessageBox.information(self, "保存完了", "フローを保存しました")
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"保存失敗: {e}")

    # ----------------------------------------------------------------- グリッド
    def _populate_grid(self) -> None:
        self._clear_grid()
        if not self._flow:
            return
        for entry in self._flow.schedule:
            try:
                row = TIME_SLOTS.index(entry.time)
            except ValueError:
                continue  # 30分境界でない時刻はスキップ
            if entry.repeat == "daily":
                for col in range(7):
                    self._set_cell(row, col, entry.target)
            elif entry.repeat == "weekly":
                for col in (entry.days or []):
                    if 0 <= col < 7:
                        self._set_cell(row, col, entry.target)

    def _clear_grid(self) -> None:
        for r in range(self.table.rowCount()):
            for c in range(7):
                self.table.setItem(r, c, None)

    def _set_cell(self, row: int, col: int, scene_path: str) -> None:
        name = os.path.basename(scene_path).removesuffix(".json")
        item = QTableWidgetItem(name)
        item.setData(Qt.UserRole, scene_path)
        item.setBackground(QBrush(_cell_color(scene_path)))
        item.setTextAlignment(Qt.AlignCenter)
        item.setToolTip(scene_path)
        self.table.setItem(row, col, item)

    def _grid_to_schedule(self) -> list[ScheduleEntry]:
        entries: list[ScheduleEntry] = []
        for row in range(self.table.rowCount()):
            for col in range(7):
                item = self.table.item(row, col)
                if item:
                    path = item.data(Qt.UserRole)
                    if path:
                        entries.append(ScheduleEntry(
                            time=TIME_SLOTS[row],
                            target=path,
                            repeat="weekly",
                            days=[col],
                        ))
        return entries

    # --------------------------------------------------------------- イベント
    def _on_cell_clicked(self, row: int, col: int) -> None:
        if not self._flow:
            QMessageBox.information(self, "情報", "先にフローを開くか新規作成してください")
            return
        dlg = _ScenePickerDialog(parent=self)
        if dlg.exec() == QDialog.Accepted:
            scene = dlg.selected()
            if scene:
                self._set_cell(row, col, scene)

    def _on_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        act_clear = menu.addAction("クリア")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action == act_clear:
            self.table.setItem(self.table.row(item), self.table.column(item), None)
