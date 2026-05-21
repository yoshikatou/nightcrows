"""トップレベルウィンドウ列挙と選択ダイアログ。"""
from __future__ import annotations

import win32con
import win32gui
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


def enum_visible_windows() -> list[tuple[int, str]]:
    """可視・タイトルあり・トップレベルウィンドウの (hwnd, title) リストを返す。"""
    items: list[tuple[int, str]] = []

    def _cb(hwnd: int, _) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        if win32gui.GetParent(hwnd) != 0:
            return True
        items.append((hwnd, title))
        return True

    win32gui.EnumWindows(_cb, None)
    return items


def find_hwnd_by_title(title: str, exact: bool = False) -> int | None:
    """タイトル一致でウィンドウハンドルを返す。複数あれば最初の1つ。"""
    if not title:
        return None
    for hwnd, t in enum_visible_windows():
        if exact:
            if t == title:
                return hwnd
        else:
            if title in t:
                return hwnd
    return None


class WindowPickerDialog(QDialog):
    """ウィンドウ一覧から選択するダイアログ。"""

    def __init__(self, current_title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ウィンドウ選択")
        self.setMinimumSize(560, 420)
        self._selected_title: str = current_title
        self._build_ui()
        self._reload()
        if current_title:
            self._filter.setText(current_title)

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)

        hint = QLabel("ゲームウィンドウを選択してください（部分一致で絞り込み可）")
        hint.setStyleSheet("color:#555; font-size:11px;")
        lay.addWidget(hint)

        row = QHBoxLayout()
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("タイトル絞り込み…")
        self._filter.textChanged.connect(self._apply_filter)
        btn_reload = QPushButton("🔄 再取得")
        btn_reload.clicked.connect(self._reload)
        row.addWidget(self._filter, 1)
        row.addWidget(btn_reload)
        lay.addLayout(row)

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda *_: self.accept())
        lay.addWidget(self._list, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _reload(self) -> None:
        self._all = enum_visible_windows()
        self._apply_filter()

    def _apply_filter(self) -> None:
        q = self._filter.text().strip().lower()
        self._list.clear()
        for hwnd, title in self._all:
            if q and q not in title.lower():
                continue
            it = QListWidgetItem(f"{title}    [hwnd=0x{hwnd:X}]")
            it.setData(Qt.UserRole, (hwnd, title))
            self._list.addItem(it)

    def accept(self) -> None:
        it = self._list.currentItem()
        if it is None and self._list.count() > 0:
            it = self._list.item(0)
        if it is None:
            return
        _, title = it.data(Qt.UserRole)
        self._selected_title = title
        super().accept()

    def selected_title(self) -> str:
        return self._selected_title
