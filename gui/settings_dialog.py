"""デバイス一覧（ラベル + IP）を編集するダイアログ。"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .mdns_dialog import MdnsDialog
from .settings import AppSettings, Device


class DeviceSettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("デバイス設定")
        self.resize(520, 360)

        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["ラベル", "IP"])
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(False)

        for d in settings.devices:
            self._append_row(d.label, d.ip)

        row = QHBoxLayout()
        btn_add = QPushButton("追加")
        btn_add.clicked.connect(lambda: self._append_row("", ""))
        btn_del = QPushButton("削除")
        btn_del.clicked.connect(self._remove_selected)
        btn_mdns = QPushButton("🔍 mDNS 検出")
        btn_mdns.clicked.connect(self._discover_mdns)
        row.addWidget(btn_add)
        row.addWidget(btn_del)
        row.addWidget(btn_mdns)
        row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._table)
        layout.addLayout(row)
        layout.addWidget(buttons)

        self._result: AppSettings | None = None

    def _append_row(self, label: str, ip: str) -> None:
        r = self._table.rowCount()
        self._table.insertRow(r)
        self._table.setItem(r, 0, QTableWidgetItem(label))
        self._table.setItem(r, 1, QTableWidgetItem(ip))

    def _remove_selected(self) -> None:
        rows = sorted({idx.row() for idx in self._table.selectedIndexes()}, reverse=True)
        for r in rows:
            self._table.removeRow(r)

    def _discover_mdns(self) -> None:
        dlg = MdnsDialog(self)
        if dlg.exec() != dlg.Accepted:
            return
        picked = dlg.selected()
        if not picked:
            return
        ip, _port = picked
        sel_rows = sorted({idx.row() for idx in self._table.selectedIndexes()})
        if sel_rows:
            r = sel_rows[0]
            label_item = self._table.item(r, 0)
            if label_item is None or not label_item.text().strip():
                self._table.setItem(r, 0, QTableWidgetItem(ip))
            self._table.setItem(r, 1, QTableWidgetItem(ip))
        else:
            self._append_row(ip, ip)

    def _cell(self, r: int, c: int) -> str:
        item = self._table.item(r, c)
        return (item.text() if item else "").strip()

    def _on_ok(self) -> None:
        devices: list[Device] = []
        for r in range(self._table.rowCount()):
            label = self._cell(r, 0)
            ip = self._cell(r, 1)
            if not label or not ip:
                QMessageBox.warning(self, "エラー",
                                    f"{r + 1} 行目: ラベルと IP は必須です")
                return
            devices.append(Device(label=label, ip=ip))
        self._result = AppSettings(devices=devices)
        self.accept()

    def result_settings(self) -> AppSettings | None:
        return self._result
