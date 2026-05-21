"""メンテナンス日程管理ダイアログ。"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (
    QDateTimeEdit, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from .maintenance import MaintenanceEntry, new_entry, load_maintenance, save_maintenance


class _EntryDialog(QDialog):
    """メンテナンス1件の追加・編集ダイアログ。"""

    def __init__(self, entry: MaintenanceEntry | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("メンテナンス登録" if entry is None else "メンテナンス編集")
        self.setMinimumWidth(360)

        now = datetime.now().replace(second=0, microsecond=0)
        form = QFormLayout(self)

        self.label_edit = QLineEdit(entry.label if entry else "")
        self.label_edit.setPlaceholderText("例：定期メンテナンス")
        form.addRow("内容:", self.label_edit)

        def _to_qdt(s: str) -> QDateTime:
            try:
                dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
            except ValueError:
                dt = now
            return QDateTime(dt.year, dt.month, dt.day, dt.hour, dt.minute, 0)

        self.start_edit = QDateTimeEdit(_to_qdt(entry.start) if entry else QDateTime(
            now.year, now.month, now.day, now.hour, now.minute, 0))
        self.start_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.start_edit.setCalendarPopup(True)
        form.addRow("開始:", self.start_edit)

        self.end_edit = QDateTimeEdit(_to_qdt(entry.end) if entry else QDateTime(
            now.year, now.month, now.day, now.hour + 1, now.minute, 0))
        self.end_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.end_edit.setCalendarPopup(True)
        form.addRow("終了:", self.end_edit)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _on_ok(self) -> None:
        if not self.label_edit.text().strip():
            QMessageBox.warning(self, "入力エラー", "内容を入力してください")
            return
        s = self.start_edit.dateTime().toPython()
        e = self.end_edit.dateTime().toPython()
        if e <= s:
            QMessageBox.warning(self, "入力エラー", "終了は開始より後にしてください")
            return
        self.accept()

    def get_values(self) -> tuple[str, str, str]:
        label = self.label_edit.text().strip()
        start = self.start_edit.dateTime().toString("yyyy-MM-dd HH:mm")
        end = self.end_edit.dateTime().toString("yyyy-MM-dd HH:mm")
        return label, start, end


class MaintenanceDialog(QDialog):
    """メンテナンス一覧の管理ダイアログ。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("メンテナンス日程")
        self.setMinimumSize(500, 340)
        self._entries: list[MaintenanceEntry] = load_maintenance()

        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("登録済みメンテナンス:"))
        self.list_widget = QListWidget()
        lay.addWidget(self.list_widget, 1)

        row = QHBoxLayout()
        btn_add = QPushButton("追加")
        btn_add.clicked.connect(self._add)
        btn_edit = QPushButton("編集")
        btn_edit.clicked.connect(self._edit)
        btn_del = QPushButton("削除")
        btn_del.clicked.connect(self._delete)
        row.addWidget(btn_add)
        row.addWidget(btn_edit)
        row.addWidget(btn_del)
        row.addStretch()
        lay.addLayout(row)

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(self.accept)
        lay.addWidget(btns)

        self._refresh()

    def _refresh(self) -> None:
        self.list_widget.clear()
        now = datetime.now()
        for e in self._entries:
            try:
                s = datetime.strptime(e.start, "%Y-%m-%d %H:%M")
                t = datetime.strptime(e.end, "%Y-%m-%d %H:%M")
                active = s <= now < t
            except ValueError:
                active = False
            label = f"{'[実施中] ' if active else ''}{e.label}  {e.start} 〜 {e.end}"
            item = QListWidgetItem(label)
            if active:
                item.setForeground(Qt.red)
            self.list_widget.addItem(item)

    def _add(self) -> None:
        dlg = _EntryDialog(parent=self)
        if dlg.exec() == QDialog.Accepted:
            label, start, end = dlg.get_values()
            self._entries.append(new_entry(label, start, end))
            save_maintenance(self._entries)
            self._refresh()

    def _edit(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0:
            return
        dlg = _EntryDialog(entry=self._entries[row], parent=self)
        if dlg.exec() == QDialog.Accepted:
            label, start, end = dlg.get_values()
            e = self._entries[row]
            e.label, e.start, e.end = label, start, end
            save_maintenance(self._entries)
            self._refresh()

    def _delete(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0:
            return
        e = self._entries[row]
        r = QMessageBox.question(self, "確認", f"削除しますか？\n{e.label}  {e.start} 〜 {e.end}")
        if r == QMessageBox.Yes:
            self._entries.pop(row)
            save_maintenance(self._entries)
            self._refresh()
