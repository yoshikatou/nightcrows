"""mDNS で見つかった ADB デバイスを一覧表示して選択させるダイアログ。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout,
)

from .adb import adb_mdns_services


class MdnsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("mDNS 検出")
        self.resize(560, 320)

        self._list = QListWidget()
        self._services: list[dict] = []

        btn_refresh = QPushButton("🔄 更新")
        btn_refresh.clicked.connect(self._refresh)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        top = QHBoxLayout()
        top.addWidget(QLabel("見つかったデバイス（選択して OK）:"))
        top.addStretch(1)
        top.addWidget(btn_refresh)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self._list)
        layout.addWidget(buttons)

        self._list.itemDoubleClicked.connect(lambda _: self.accept())

        self._refresh()

    def _refresh(self) -> None:
        self._services = adb_mdns_services()
        self._list.clear()
        # tls-connect を優先表示（無線デバッグ接続用）
        connect_services = [s for s in self._services if "connect" in s.get("service", "")]
        other_services = [s for s in self._services if s not in connect_services]
        ordered = connect_services + other_services
        self._services = ordered

        for s in ordered:
            text = f"{s['ip']}:{s['port']}    [{s['service']}]    {s['name']}"
            item = QListWidgetItem(text)
            self._list.addItem(item)
        if not ordered:
            self._list.addItem("(見つかりませんでした)")
            self._list.item(0).setFlags(Qt.NoItemFlags)

    def selected(self) -> tuple[str, str] | None:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._services):
            return None
        s = self._services[row]
        ip = s.get("ip", "")
        port = s.get("port", "")
        if not ip or not port:
            return None
        return ip, port
