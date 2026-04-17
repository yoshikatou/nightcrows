"""デバイス一覧を編集するダイアログ。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .settings import AppSettings, Device


class DeviceSettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("デバイス設定")
        self.resize(520, 360)

        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["ラベル", "シリアル"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(False)

        for d in settings.devices:
            self._append_row(d.label, d.serial)

        row = QHBoxLayout()
        btn_add = QPushButton("追加")
        btn_add.clicked.connect(lambda: self._append_row("", ""))
        btn_del = QPushButton("削除")
        btn_del.clicked.connect(self._remove_selected)
        row.addWidget(btn_add)
        row.addWidget(btn_del)
        row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._table)
        layout.addLayout(row)
        layout.addWidget(buttons)

        self._result: AppSettings | None = None

    def _append_row(self, label: str, serial: str) -> None:
        r = self._table.rowCount()
        self._table.insertRow(r)
        self._table.setItem(r, 0, QTableWidgetItem(label))
        self._table.setItem(r, 1, QTableWidgetItem(serial))

    def _remove_selected(self) -> None:
        rows = sorted({idx.row() for idx in self._table.selectedIndexes()}, reverse=True)
        for r in rows:
            self._table.removeRow(r)

    def _on_ok(self) -> None:
        devices: list[Device] = []
        for r in range(self._table.rowCount()):
            label = (self._table.item(r, 0).text() if self._table.item(r, 0) else "").strip()
            serial = (self._table.item(r, 1).text() if self._table.item(r, 1) else "").strip()
            if not label or not serial:
                QMessageBox.warning(self, "エラー",
                                    f"{r + 1} 行目: ラベルとシリアルは必須です")
                return
            devices.append(Device(label=label, serial=serial))
        self._result = AppSettings(devices=devices)
        self.accept()

    def result_settings(self) -> AppSettings | None:
        return self._result
