"""フロー編集タブ — TV番組表スタイルの週間スケジュールエディタ。

列 = 曜日（月〜日）、行 = 時刻（30分刻み）。
セルをクリックしてエントリを管理、右クリックでクリア。

エントリには2種類ある:
  timed  : {"time": "HH:MM", "scene": "..."}   — 指定時刻に発火
  seq    : {"seq": True,     "scene": "..."}   — 直前エントリ終了後に続けて実行
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime as _dt

from PySide6.QtCore import Qt, QEvent, QPoint, QTime, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox,
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
    QTimeEdit, QVBoxLayout, QWidget,
)

from .flow import Flow, ScheduleEntry, Watcher, load_flow, save_flow, load_watchers_dir, save_watcher
from .settings import save_settings

WATCHERS_DIR = "watchers"

FLOWS_DIR = "flows"
SCENES_DIR = "scenes"

DAYS = ["月", "火", "水", "木", "金", "土", "日"]

_SLOT_MIN = 30


def _make_slots() -> list[str]:
    slots = []
    for h in range(24):
        for m in range(0, 60, _SLOT_MIN):
            slots.append(f"{h:02d}:{m:02d}")
    return slots


TIME_SLOTS = _make_slots()

_TODAY_HDR_BG  = QColor("#FFF176")
_TODAY_CELL_BG = QColor("#FFFDE7")

_PALETTE = [
    "#BBDEFB", "#C8E6C9", "#FFE0B2", "#F8BBD0",
    "#E1BEE7", "#B2DFDB", "#FFF9C4", "#D7CCC8",
    "#B3E5FC", "#DCEDC8", "#FFCDD2", "#CFD8DC",
]


def _cell_color(scene_path: str) -> QColor:
    return QColor(_PALETTE[abs(hash(scene_path)) % len(_PALETTE)])


# ------------------------------------------------------------------ シーン選択
class _ScenePickerDialog(QDialog):
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


# ------------------------------------------------------------------ 時刻+シーン入力
class _TimedEntryDialog(QDialog):
    """時刻とシーンを選択するダイアログ（時間指定エントリ用）。"""

    def __init__(self, slot_time: str, scene: str = "", exact_time: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("時間指定エントリ")
        self.setMinimumSize(400, 460)

        lay = QVBoxLayout(self)
        form = QFormLayout()
        h, m = map(int, (exact_time or slot_time).split(":"))
        self.time_edit = QTimeEdit(QTime(h, m))
        self.time_edit.setDisplayFormat("HH:mm")
        self.time_edit.setWrapping(True)
        form.addRow("実行時刻:", self.time_edit)
        lay.addLayout(form)

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
        self._load(scene)

    def _load(self, preselect: str) -> None:
        if os.path.isdir(SCENES_DIR):
            for root, _, files in os.walk(SCENES_DIR):
                for f in sorted(files):
                    if f.endswith(".json"):
                        rel = os.path.relpath(
                            os.path.join(root, f), SCENES_DIR
                        ).replace("\\", "/")
                        self._all.append(rel)
        self._filter("")
        if preselect:
            items = self.list.findItems(preselect, Qt.MatchExactly)
            if items:
                self.list.setCurrentItem(items[0])

    def _filter(self, text: str) -> None:
        self.list.clear()
        for s in self._all:
            if text.lower() in s.lower():
                self.list.addItem(s)
        if self.list.count() > 0 and self.list.currentRow() < 0:
            self.list.setCurrentRow(0)

    def _on_ok(self) -> None:
        if not self.list.currentItem():
            return
        self.accept()

    def get_values(self) -> tuple[str, str]:
        t = self.time_edit.time()
        return f"{t.hour():02d}:{t.minute():02d}", (
            self.list.currentItem().text() if self.list.currentItem() else ""
        )


# ------------------------------------------------------------------ スロット管理ダイアログ
_SEQ_FG = QColor("#1565C0")   # 続き実行エントリの文字色（青）


class _SlotEntriesDialog(QDialog):
    """1つの時間枠のエントリを管理するダイアログ。

    エントリの種類:
      {"time": "HH:MM", "scene": "..."} — 時間指定（指定時刻に発火）
      {"seq": True,     "scene": "..."} — 続きで実行（直前エントリ終了後）
    """

    def __init__(self, slot_time: str, entries: list[dict],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"スケジュール管理 — {slot_time} 枠")
        self.setMinimumSize(500, 380)
        self._entries: list[dict] = [dict(e) for e in entries]
        self._slot_time = slot_time

        lay = QVBoxLayout(self)
        legend = QLabel(
            "🕐 時間指定: 指定時刻に発火    🔵 続きで実行: 直前のシーン終了後にすぐ実行"
        )
        legend.setStyleSheet("font-size: 10px; color: #555;")
        lay.addWidget(legend)

        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.currentRowChanged.connect(self._on_sel)
        lay.addWidget(self.list, 1)

        btn_row = QHBoxLayout()
        self.btn_add_timed = QPushButton("🕐 時間指定で追加")
        self.btn_add_seq   = QPushButton("🔵 続きで実行を追加")
        self.btn_up        = QPushButton("↑")
        self.btn_down      = QPushButton("↓")
        self.btn_edit      = QPushButton("✎ 編集")
        self.btn_del       = QPushButton("✕ 削除")
        self.btn_add_timed.clicked.connect(self._add_timed)
        self.btn_add_seq.clicked.connect(self._add_seq)
        self.btn_up.clicked.connect(self._move_up)
        self.btn_down.clicked.connect(self._move_down)
        self.btn_edit.clicked.connect(self._edit)
        self.btn_del.clicked.connect(self._delete)
        for b in (self.btn_add_timed, self.btn_add_seq,
                  self.btn_up, self.btn_down, self.btn_edit, self.btn_del):
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._refresh()

    def _item_text(self, e: dict) -> str:
        name = os.path.basename(e["scene"]).removesuffix(".json")
        if e.get("seq"):
            return f"  → {name}  （続きで実行）"
        return f"🕐 {e['time']}  {name}"

    def _refresh(self) -> None:
        row = self.list.currentRow()
        self.list.clear()
        for e in self._entries:
            item = QListWidgetItem(self._item_text(e))
            if e.get("seq"):
                item.setForeground(_SEQ_FG)
            item.setData(Qt.UserRole, e)
            self.list.addItem(item)
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)
        self._on_sel(self.list.currentRow())

    def _on_sel(self, row: int) -> None:
        has = row >= 0
        n = self.list.count()
        self.btn_up.setEnabled(has and row > 0)
        self.btn_down.setEnabled(has and row < n - 1)
        self.btn_edit.setEnabled(has)
        self.btn_del.setEnabled(has)

    def _add_timed(self) -> None:
        dlg = _TimedEntryDialog(slot_time=self._slot_time, parent=self)
        if dlg.exec() == QDialog.Accepted:
            t, scene = dlg.get_values()
            if scene:
                self._entries.append({"time": t, "scene": scene})
                self._refresh()
                self.list.setCurrentRow(len(self._entries) - 1)

    def _add_seq(self) -> None:
        dlg = _ScenePickerDialog(parent=self)
        if dlg.exec() == QDialog.Accepted and dlg.selected():
            self._entries.append({"seq": True, "scene": dlg.selected()})
            self._refresh()
            self.list.setCurrentRow(len(self._entries) - 1)

    def _edit(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        e = self._entries[row]
        if e.get("seq"):
            dlg = _ScenePickerDialog(parent=self)
            # preselect current scene
            if dlg.exec() == QDialog.Accepted and dlg.selected():
                self._entries[row] = {"seq": True, "scene": dlg.selected()}
                self._refresh()
        else:
            dlg = _TimedEntryDialog(slot_time=self._slot_time,
                                    scene=e["scene"], exact_time=e["time"],
                                    parent=self)
            if dlg.exec() == QDialog.Accepted:
                t, scene = dlg.get_values()
                if scene:
                    self._entries[row] = {"time": t, "scene": scene}
                    self._refresh()

    def _move_up(self) -> None:
        row = self.list.currentRow()
        if row <= 0:
            return
        self._entries[row - 1], self._entries[row] = self._entries[row], self._entries[row - 1]
        self._refresh()
        self.list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self.list.currentRow()
        if row < 0 or row >= len(self._entries) - 1:
            return
        self._entries[row], self._entries[row + 1] = self._entries[row + 1], self._entries[row]
        self._refresh()
        self.list.setCurrentRow(row + 1)

    def _delete(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        self._entries.pop(row)
        self._refresh()

    def get_entries(self) -> list[dict]:
        return list(self._entries)


# ------------------------------------------------------------------ 現在時刻横線オーバーレイ
class _TimeLineOverlay(QWidget):
    """グリッドビューポート上に現在時刻の赤横線を描く透明オーバーレイ。"""

    def __init__(self, table: "QTableWidget") -> None:
        super().__init__(table.viewport())
        self._table = table
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setGeometry(table.viewport().rect())
        self.raise_()

    def paintEvent(self, _event) -> None:
        now = _dt.now()
        total_min = now.hour * 60 + now.minute
        row_idx = total_min // _SLOT_MIN
        if row_idx >= self._table.rowCount():
            return
        fraction = (total_min % _SLOT_MIN) / _SLOT_MIN

        rect = self._table.visualRect(self._table.model().index(row_idx, 0))
        if not rect.isValid():
            return
        y = rect.top() + int(fraction * rect.height())
        if y < 0 or y > self.height():
            return

        p = QPainter(self)
        red = QColor("#e53935")
        pen = QPen(red)
        pen.setWidth(2)
        p.setPen(pen)
        p.drawLine(8, y, self.width(), y)
        p.setBrush(red)
        p.setPen(Qt.NoPen)
        p.drawEllipse(0, y - 4, 8, 8)
        p.end()


# ------------------------------------------------------------------ ドラッグ対応テーブル
_DRAG_THRESHOLD = 6


class _ScheduleTable(QTableWidget):
    cell_drag_moved = Signal(int, int, int, int)

    def __init__(self, rows: int, cols: int, parent=None) -> None:
        super().__init__(rows, cols, parent)
        self._drag_src: tuple[int, int] | None = None
        self._drag_start_pos: QPoint | None = None
        self._dragging = False
        self._overlay = _TimeLineOverlay(self)
        self.viewport().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.viewport() and event.type() == QEvent.Type.Resize:
            self._overlay.setGeometry(obj.rect())
            self._overlay.raise_()
        return super().eventFilter(obj, event)

    def refresh_time_line(self) -> None:
        self._overlay.raise_()
        self._overlay.update()

    def scroll_to_now(self) -> None:
        """現在時刻の行をビューポート中央にスクロールする。"""
        now = _dt.now()
        row_idx = (now.hour * 60 + now.minute) // _SLOT_MIN
        if row_idx < self.rowCount():
            self.scrollTo(
                self.model().index(row_idx, 0),
                QAbstractItemView.PositionAtCenter,
            )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            item = self.itemAt(pos)
            if item is not None:
                entries = item.data(Qt.UserRole)
                if entries:
                    self._drag_src = (self.row(item), self.column(item))
                    self._drag_start_pos = pos
                    self._dragging = False
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start_pos is not None and not self._dragging:
            delta = event.position().toPoint() - self._drag_start_pos
            if delta.manhattanLength() >= _DRAG_THRESHOLD:
                self._dragging = True
                self.setCursor(QCursor(Qt.DragMoveCursor))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_src is not None:
            if self._dragging:
                pos = event.position().toPoint()
                dst_row = self.rowAt(pos.y())
                dst_col = self.columnAt(pos.x())
                if dst_row >= 0 and dst_col >= 0:
                    sr, sc = self._drag_src
                    if (sr, sc) != (dst_row, dst_col):
                        self.cell_drag_moved.emit(sr, sc, dst_row, dst_col)
                self.unsetCursor()
                self._drag_src = None
                self._drag_start_pos = None
                self._dragging = False
                return
            self.unsetCursor()
            self._drag_src = None
            self._drag_start_pos = None
            self._dragging = False
        super().mouseReleaseEvent(event)


# ------------------------------------------------------------------ メイン
class FlowEditorWidget(QWidget):

    def __init__(self, main_window) -> None:
        super().__init__()
        self._mw = main_window
        self._flow: Flow | None = None
        self._flow_path: str | None = None
        self._copy_buffer: list[dict] | None = None
        self._build_ui()
        self._restore_last_flow()

    # ----------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setSpacing(6)

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
        btn_dup = QPushButton("複製")
        btn_dup.clicked.connect(self._duplicate)
        self.btn_save = QPushButton("保存")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save)
        for w in (btn_open, btn_new, btn_dup, self.btn_save):
            row1.addWidget(w)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("フロー名:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("untitled")
        self.name_edit.textChanged.connect(self._autosave)
        row2.addWidget(self.name_edit, 1)
        self.save_status_label = QLabel("")
        self.save_status_label.setStyleSheet("color: #388e3c; font-size: 10px;")
        row2.addWidget(self.save_status_label)
        lay.addLayout(row2)

        hint = QLabel(
            "左クリック：エントリ管理（🕐時間指定 / 🔵続きで実行）　右クリック：枠をクリア"
        )
        hint.setStyleSheet("color: #666; font-size: 10px;")
        lay.addWidget(hint)

        self.table = _ScheduleTable(len(TIME_SLOTS), 7)
        self.table.setHorizontalHeaderLabels(DAYS)
        self.table.setVerticalHeaderLabels(TIME_SLOTS)

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)

        vh = self.table.verticalHeader()
        vh.setDefaultSectionSize(26)
        vh.setSectionResizeMode(QHeaderView.ResizeToContents)

        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.cell_drag_moved.connect(self._on_cell_drag_moved)
        lay.addWidget(self.table, 1)
        self._highlight_today()

        # 現在時刻自動追従チェックボックス
        time_row = QHBoxLayout()
        self.chk_auto_scroll = QCheckBox("現在時刻に自動追従")
        self.chk_auto_scroll.setChecked(True)
        self.chk_auto_scroll.toggled.connect(self._on_auto_scroll_toggled)
        btn_goto_now = QPushButton("今すぐ移動")
        btn_goto_now.setFixedWidth(90)
        btn_goto_now.clicked.connect(self._goto_now)
        time_row.addWidget(self.chk_auto_scroll)
        time_row.addWidget(btn_goto_now)
        time_row.addStretch()
        lay.addLayout(time_row)

        # 30秒ごとに現在時刻線を更新
        self._time_timer = QTimer(self)
        self._time_timer.timeout.connect(self._on_time_tick)
        self._time_timer.start(30_000)

        # 起動時に現在時刻へスクロール
        QTimer.singleShot(200, self._goto_now)

        # ウォッチャータグバー
        tag_header = QHBoxLayout()
        tag_lbl = QLabel("ウォッチャー:")
        tag_lbl.setStyleSheet("font-weight: bold; font-size: 10px; color: #444;")
        tag_header.addWidget(tag_lbl)
        tag_header.addStretch()
        lay.addLayout(tag_header)

        self._tag_scroll = QScrollArea()
        self._tag_scroll.setFrameShape(QScrollArea.NoFrame)
        self._tag_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._tag_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tag_scroll.setFixedHeight(36)
        self._tag_scroll.setWidgetResizable(True)

        self._tag_container = QWidget()
        self._tag_layout = QHBoxLayout(self._tag_container)
        self._tag_layout.setContentsMargins(0, 0, 0, 0)
        self._tag_layout.setSpacing(6)
        self._tag_layout.addStretch()
        self._tag_scroll.setWidget(self._tag_container)
        lay.addWidget(self._tag_scroll)

        self.refresh_watcher_tags()

    # -------------------------------------------------------------- 時刻追従
    def _on_time_tick(self) -> None:
        self.table.refresh_time_line()
        if self.chk_auto_scroll.isChecked():
            self.table.scroll_to_now()

    def _on_auto_scroll_toggled(self, checked: bool) -> None:
        if checked:
            self.table.scroll_to_now()

    def _goto_now(self) -> None:
        self.table.scroll_to_now()
        self.table.refresh_time_line()

    # -------------------------------------------------------------- ウォッチャータグ
    def refresh_watcher_tags(self) -> None:
        """ウォッチャーディレクトリを再読み込みしてタグボタンを再構築する。"""
        # 既存ボタンを全削除（stretchは後で追加）
        while self._tag_layout.count():
            item = self._tag_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        os.makedirs(WATCHERS_DIR, exist_ok=True)
        pairs = load_watchers_dir(WATCHERS_DIR)
        for path, w in pairs:
            label = w.title or w.id
            if w.condition.type in ("ocr_number", "digit_threshold"):
                label = f"{label}  {w.condition.op}{w.condition.value}"
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(w.enabled)
            btn.setProperty("watcher_id", w.id)
            btn.setProperty("watcher_path", path)
            btn.setStyleSheet("""
                QPushButton {
                    border-radius: 11px;
                    padding: 3px 12px;
                    font-weight: bold;
                    font-size: 11px;
                    border: none;
                }
                QPushButton:checked {
                    background: #1565c0;
                    color: white;
                }
                QPushButton:!checked {
                    background: #cfd8dc;
                    color: #607d8b;
                    font-weight: normal;
                }
                QPushButton:checked:hover { background: #0d47a1; }
                QPushButton:!checked:hover { background: #b0bec5; }
            """)
            btn.toggled.connect(
                lambda checked, wid=w.id, p=path: self._on_tag_toggled(wid, p, checked)
            )
            self._tag_layout.addWidget(btn)

        self._tag_layout.addStretch()

    def _on_tag_toggled(self, watcher_id: str, path: str, enabled: bool) -> None:
        """タグクリックでウォッチャーの有効/無効を切り替えて保存する。"""
        watcher_editor = getattr(self._mw, "watcher_editor", None)
        if watcher_editor is not None:
            watcher_editor.toggle_watcher_by_id(watcher_id, enabled)
        else:
            pairs = load_watchers_dir(WATCHERS_DIR)
            for p, w in pairs:
                if w.id == watcher_id and p == path:
                    w.enabled = enabled
                    save_watcher(w, p)
                    break

    # -------------------------------------------------------------- 最終フロー復元
    def _restore_last_flow(self) -> None:
        path = self._mw.settings.last_flow
        if not path or not os.path.exists(path):
            return
        try:
            flow = load_flow(path)
        except Exception:
            return
        self._flow = flow
        self._flow_path = path
        self.path_edit.setText(path)
        self.name_edit.blockSignals(True)
        self.name_edit.setText(flow.name)
        self.name_edit.blockSignals(False)
        self._populate_grid()
        self.btn_save.setEnabled(True)

    def _save_last_flow(self, path: str) -> None:
        if self._mw.settings.last_flow != path:
            self._mw.settings.last_flow = path
            save_settings(self._mw.settings)

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
        self.name_edit.blockSignals(True)
        self.name_edit.setText(flow.name)
        self.name_edit.blockSignals(False)
        self._populate_grid()
        self.btn_save.setEnabled(True)
        self._save_last_flow(path)

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
        self.name_edit.blockSignals(True)
        self.name_edit.setText(name)
        self.name_edit.blockSignals(False)
        self._clear_grid()
        self._highlight_today()
        self.btn_save.setEnabled(True)
        self._save_last_flow(path)

    def _duplicate(self) -> None:
        if not self._flow or not self._flow_path:
            QMessageBox.information(self, "情報", "先にフローを開いてください")
            return
        self._flow.name = self.name_edit.text().strip() or "untitled"
        weekly = self._grid_to_schedule()
        other = [e for e in self._flow.schedule if e.repeat != "weekly"]
        self._flow.schedule = other + weekly

        base = os.path.splitext(self._flow_path)[0]
        path, _ = QFileDialog.getSaveFileName(
            self, "複製先を選択", f"{base}_copy.json", "JSON (*.json)")
        if not path:
            return
        new_name = os.path.splitext(os.path.basename(path))[0]
        import copy
        new_flow = copy.deepcopy(self._flow)
        new_flow.name = new_name
        try:
            save_flow(new_flow, path)
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"保存失敗: {e}")
            return
        self._flow = new_flow
        self._flow_path = path
        self.path_edit.setText(path)
        self.name_edit.blockSignals(True)
        self.name_edit.setText(new_name)
        self.name_edit.blockSignals(False)
        self.btn_save.setEnabled(True)
        self._save_last_flow(path)
        QMessageBox.information(self, "複製完了", f"複製しました:\n{path}")

    def _autosave(self) -> None:
        if not self._flow or not self._flow_path:
            return
        self._flow.name = self.name_edit.text().strip() or "untitled"
        weekly = self._grid_to_schedule()
        other = [e for e in self._flow.schedule if e.repeat != "weekly"]
        self._flow.schedule = other + weekly
        try:
            save_flow(self._flow, self._flow_path)
            self.save_status_label.setText("✓ 自動保存済")
        except Exception:
            self.save_status_label.setText("⚠ 保存失敗")
            self.save_status_label.setStyleSheet("color: #c62828; font-size: 10px;")

    def _save(self) -> None:
        if not self._flow or not self._flow_path:
            return
        self._autosave()
        QMessageBox.information(self, "保存完了", "フローを保存しました")

    # ----------------------------------------------------------------- グリッド
    def _entries_from_schedule(self) -> dict[tuple[int, int], list[dict]]:
        """ScheduleEntry リストをセル別エントリリストに変換する。"""
        result: dict[tuple[int, int], list[dict]] = defaultdict(list)
        for entry in (self._flow.schedule if self._flow else []):
            try:
                h, m = map(int, entry.time.split(":"))
            except ValueError:
                continue
            row = (h * 60 + m) // _SLOT_MIN
            if row >= len(TIME_SLOTS):
                continue
            cols = list(range(7)) if entry.repeat == "daily" else (entry.days or [])
            for col in cols:
                if 0 <= col < 7:
                    if entry.target:
                        result[(row, col)].append(
                            {"time": entry.time, "scene": entry.target,
                             "enabled": entry.enabled}
                        )
                    for s in (entry.sequence or []):
                        result[(row, col)].append(
                            {"seq": True, "scene": s, "enabled": entry.enabled}
                        )
        return result

    def _populate_grid(self) -> None:
        self._clear_grid()
        if not self._flow:
            return
        for (row, col), entries in self._entries_from_schedule().items():
            self._refresh_cell(row, col, entries)
        self._highlight_today()
        self.table.refresh_time_line()

    def _highlight_today(self) -> None:
        today = _dt.now().weekday()
        for col, day in enumerate(DAYS):
            hdr = QTableWidgetItem(day)
            if col == today:
                hdr.setBackground(QBrush(_TODAY_HDR_BG))
                f = QFont(); f.setBold(True); hdr.setFont(f)
            self.table.setHorizontalHeaderItem(col, hdr)
        for row in range(self.table.rowCount()):
            if self.table.item(row, today) is None:
                ph = QTableWidgetItem()
                ph.setBackground(QBrush(_TODAY_CELL_BG))
                ph.setData(Qt.UserRole, [])
                self.table.setItem(row, today, ph)

    def _clear_grid(self) -> None:
        for r in range(self.table.rowCount()):
            for c in range(7):
                self.table.setItem(r, c, None)

    def _refresh_cell(self, row: int, col: int, entries: list[dict]) -> None:
        real = [e for e in entries if e.get("scene")]
        if not real:
            self.table.setItem(row, col, None)
            return
        disabled = all(not e.get("enabled", True) for e in real)
        lines = []
        for e in real:
            name = os.path.basename(e["scene"]).removesuffix(".json")
            prefix = "→ " if e.get("seq") else f"{e['time']} "
            mark = "⊘ " if not e.get("enabled", True) else ""
            lines.append(f"{mark}{prefix}{name}")
        first_scene = next((e["scene"] for e in real if not e.get("seq")), real[0]["scene"])
        item = QTableWidgetItem()
        item.setText("\n".join(lines))
        item.setData(Qt.UserRole, real)
        if disabled:
            item.setBackground(QBrush(QColor("#E0E0E0")))
            item.setForeground(QBrush(QColor("#9E9E9E")))
        else:
            item.setBackground(QBrush(_cell_color(first_scene)))
            item.setForeground(QBrush(QColor("#000000")))
        item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        tip_lines = []
        for e in real:
            prefix = "→ " if e.get("seq") else f"{e.get('time','')}  "
            state = " 【無効】" if not e.get("enabled", True) else ""
            tip_lines.append(f"{prefix}{e['scene']}{state}")
        item.setToolTip("\n".join(tip_lines))
        self.table.setItem(row, col, item)

    def _grid_to_schedule(self) -> list[ScheduleEntry]:
        """セルのエントリリストを ScheduleEntry リストに変換する。

        timed エントリが seq エントリの「親」となり、sequence に収める。
        """
        result: list[ScheduleEntry] = []
        for row in range(self.table.rowCount()):
            for col in range(7):
                item = self.table.item(row, col)
                if not item:
                    continue
                entries: list[dict] = item.data(Qt.UserRole) or []
                if not entries:
                    continue
                # timed エントリを起点にグループ化
                groups: list[dict] = []  # {"time", "target", "sequence"}
                for e in entries:
                    if e.get("seq"):
                        if groups:
                            groups[-1]["sequence"].append(e["scene"])
                        else:
                            # timed エントリが先にない場合: slot 時刻を使う
                            groups.append({
                                "time": TIME_SLOTS[row],
                                "target": e["scene"],
                                "sequence": [],
                            })
                    else:
                        groups.append({
                            "time": e["time"],
                            "target": e["scene"],
                            "sequence": [],
                        })
                for g in groups:
                    result.append(ScheduleEntry(
                        time=g["time"],
                        target=g["target"],
                        sequence=g["sequence"],
                        repeat="weekly",
                        days=[col],
                        enabled=g.get("enabled", True),
                    ))
        return result

    # --------------------------------------------------------------- イベント
    def _on_cell_clicked(self, row: int, col: int) -> None:
        if not self._flow:
            QMessageBox.information(self, "情報", "先にフローを開くか新規作成してください")
            return
        existing = self.table.item(row, col)
        entries = (existing.data(Qt.UserRole) or []) if existing else []
        dlg = _SlotEntriesDialog(TIME_SLOTS[row], entries, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self._refresh_cell(row, col, dlg.get_entries())
            self._autosave()

    def _on_cell_drag_moved(self, sr: int, sc: int, dr: int, dc: int) -> None:
        src_item = self.table.item(sr, sc)
        if src_item is None:
            return
        src_entries: list[dict] = src_item.data(Qt.UserRole) or []
        if not src_entries:
            return
        # 移動先スロット時刻に timed エントリの時刻を更新する
        dst_slot_time = TIME_SLOTS[dr]
        updated = []
        for e in src_entries:
            if e.get("seq"):
                updated.append(dict(e))
            else:
                updated.append({**e, "time": dst_slot_time})
        dst_item = self.table.item(dr, dc)
        dst_entries: list[dict] = (dst_item.data(Qt.UserRole) or []) if dst_item else []
        merged = list(dst_entries) + updated
        self.table.setItem(sr, sc, None)
        if sc == _dt.now().weekday():
            ph = QTableWidgetItem()
            ph.setBackground(QBrush(_TODAY_CELL_BG))
            ph.setData(Qt.UserRole, [])
            self.table.setItem(sr, sc, ph)
        self._refresh_cell(dr, dc, merged)
        self._autosave()

    def _on_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        entries: list[dict] = item.data(Qt.UserRole) or []
        scenes = [e["scene"] for e in entries if e.get("scene")]

        all_disabled = bool(entries) and all(not e.get("enabled", True) for e in entries)
        menu = QMenu(self)
        act_run = menu.addAction("▶ 今すぐ実行")
        act_run.setEnabled(bool(scenes) and not all_disabled)
        menu.addSeparator()
        act_copy = menu.addAction("📋 コピー")
        act_copy.setEnabled(bool(entries))
        act_paste = menu.addAction("📌 貼り付け")
        act_paste.setEnabled(self._copy_buffer is not None)
        menu.addSeparator()
        act_toggle = menu.addAction("✓ 有効に戻す" if all_disabled else "⊘ 無効化（スキップ）")
        act_toggle.setEnabled(bool(entries))
        menu.addSeparator()
        act_clear = menu.addAction("枠をクリア（全エントリ削除）")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))

        if action == act_run:
            self._mw.runner.run_scenes_now(scenes)
            self._mw.tabs.setCurrentWidget(self._mw.runner)
        elif action == act_copy:
            self._copy_buffer = [dict(e) for e in entries]
        elif action == act_paste:
            r, c = self.table.row(item), self.table.column(item)
            import copy
            pasted = copy.deepcopy(self._copy_buffer)
            dst_slot = TIME_SLOTS[r]
            for e in pasted:
                if not e.get("seq"):
                    e["time"] = dst_slot
            self._refresh_cell(r, c, pasted)
            self._autosave()
        elif action == act_toggle:
            r, c = self.table.row(item), self.table.column(item)
            new_enabled = all_disabled  # 無効→有効、有効→無効
            toggled = [{**e, "enabled": new_enabled} for e in entries]
            self._refresh_cell(r, c, toggled)
            self._autosave()
        elif action == act_clear:
            r, c = self.table.row(item), self.table.column(item)
            self.table.setItem(r, c, None)
            if c == _dt.now().weekday():
                ph = QTableWidgetItem()
                ph.setBackground(QBrush(_TODAY_CELL_BG))
                ph.setData(Qt.UserRole, [])
                self.table.setItem(r, c, ph)
            self._autosave()
