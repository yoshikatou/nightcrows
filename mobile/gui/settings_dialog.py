"""デバイス一覧（ラベル + IP）と外部ツールパスを編集するダイアログ。"""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .adb import adb_devices, is_usb_serial
from .mdns_dialog import MdnsDialog
from .notify import send_google_chat
from .settings import AppSettings, Device


class DeviceSettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.resize(640, 560)
        # OK 時に recording/last_device/last_flow を取りこぼさないよう、元の settings を保持
        self._initial = settings

        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["ラベル", "IP / USB シリアル"])
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
        btn_usb = QPushButton("🔌 USB 検出")
        btn_usb.clicked.connect(self._discover_usb)
        row.addWidget(btn_add)
        row.addWidget(btn_del)
        row.addWidget(btn_mdns)
        row.addWidget(btn_usb)
        row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)

        # Tesseract パス設定
        grp_tools = QGroupBox("外部ツール")
        tools_lay = QFormLayout(grp_tools)
        tess_row = QHBoxLayout()
        self._tess_edit = QLineEdit(settings.tesseract_cmd)
        self._tess_edit.setPlaceholderText(
            r"例: C:\Program Files\Tesseract-OCR\tesseract.exe  （空欄 = PATH から自動検出）"
        )
        btn_tess = QPushButton("参照")
        btn_tess.setFixedWidth(50)
        btn_tess.clicked.connect(self._browse_tesseract)
        tess_row.addWidget(self._tess_edit, 1)
        tess_row.addWidget(btn_tess)
        tools_lay.addRow("Tesseract 実行ファイル:", tess_row)
        hint = QLabel("OCRウォッチャーを使う場合に指定。インストール先の tesseract.exe を選択してください。")
        hint.setStyleSheet("color: #555; font-size: 9px;")
        hint.setWordWrap(True)
        tools_lay.addRow("", hint)

        # Google Chat 通知設定
        grp_chat = QGroupBox("Google Chat 通知（ウォッチャー発火時）")
        chat_lay = QFormLayout(grp_chat)
        self._chat_edit = QLineEdit(settings.google_chat_webhook)
        self._chat_edit.setPlaceholderText(
            "https://chat.googleapis.com/v1/spaces/.../messages?key=...&token=..."
        )
        self._chat_edit.setToolTip(
            "Google Chat スペースの「アプリと連携」→「Webhook を追加」で発行した URL を貼り付け。"
            "空欄なら通知無効。"
        )
        chat_lay.addRow("Webhook URL:", self._chat_edit)

        test_row = QHBoxLayout()
        test_row.addStretch(1)
        self._btn_chat_test = QPushButton("テスト通知を送信")
        self._btn_chat_test.clicked.connect(self._on_chat_test)
        test_row.addWidget(self._btn_chat_test)
        chat_lay.addRow("", test_row)

        self._lbl_chat_test = QLabel("")
        self._lbl_chat_test.setWordWrap(True)
        self._lbl_chat_test.setStyleSheet("color:#555; font-size:11px;")
        chat_lay.addRow("", self._lbl_chat_test)

        layout = QVBoxLayout(self)
        layout.addWidget(self._table)
        layout.addLayout(row)
        layout.addWidget(grp_tools)
        layout.addWidget(grp_chat)
        layout.addWidget(buttons)

        self._result: AppSettings | None = None

    def _on_chat_test(self) -> None:
        url = self._chat_edit.text().strip()
        if not url:
            self._lbl_chat_test.setStyleSheet("color:#c62828; font-size:11px;")
            self._lbl_chat_test.setText("⚠ Webhook URL を入力してください")
            return
        self._lbl_chat_test.setStyleSheet("color:#555; font-size:11px;")
        self._lbl_chat_test.setText("送信中…")
        QApplication.processEvents()
        ok, msg = send_google_chat(
            url,
            "テスト通知 (モバイル)",
            "Nightcrows 自動化ツール（モバイル版）からのテスト送信です。\n"
            "この通知が見えていれば、Webhook 設定は正しく動いています。",
        )
        if ok:
            self._lbl_chat_test.setStyleSheet("color:#2e7d32; font-size:11px;")
            self._lbl_chat_test.setText(f"✓ 送信成功 ({msg})")
        else:
            self._lbl_chat_test.setStyleSheet("color:#c62828; font-size:11px;")
            self._lbl_chat_test.setText(f"✗ 送信失敗: {msg}")

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

    def _discover_usb(self) -> None:
        import subprocess
        from .adb import ADB

        def get_model(serial: str) -> str:
            try:
                r = subprocess.run(
                    [ADB, "-s", serial, "shell", "getprop", "ro.product.model"],
                    capture_output=True, text=True, timeout=4,
                )
                return r.stdout.strip() or serial
            except Exception:
                return serial

        devices = [(s, st) for s, st in adb_devices() if is_usb_serial(s)]
        if not devices:
            QMessageBox.information(self, "USB 検出",
                "USB 接続中のデバイスが見つかりませんでした。\n"
                "ケーブルを接続し、スマホで「USBデバッグを許可」を選んでください。")
            return

        if len(devices) == 1:
            serial, status = devices[0]
            model = get_model(serial)
            self._append_row(model, serial)
            QMessageBox.information(self, "USB 検出",
                f"デバイスを追加しました:\n{model}  /  {serial}  ({status})")
        else:
            from PySide6.QtWidgets import QInputDialog
            info = [(s, st, get_model(s)) for s, st in devices]
            items = [f"{model}  /  {s}  ({st})" for s, st, model in info]
            item, ok = QInputDialog.getItem(self, "USB デバイス選択",
                                            "追加するデバイスを選択:", items, 0, False)
            if ok and item:
                idx = items.index(item)
                serial, _, model = info[idx]
                self._append_row(model, serial)

    def _browse_tesseract(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "tesseract.exe を選択", r"C:\Program Files\Tesseract-OCR",
            "実行ファイル (*.exe)"
        )
        if path:
            self._tess_edit.setText(path)

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
        # 元の AppSettings をベースに、ダイアログで編集した項目だけ上書き。
        # こうしないと recording / last_device / last_flow が初期値で潰される。
        self._result = replace(
            self._initial,
            devices=devices,
            tesseract_cmd=self._tess_edit.text().strip(),
            google_chat_webhook=self._chat_edit.text().strip(),
        )
        self.accept()

    def result_settings(self) -> AppSettings | None:
        return self._result
