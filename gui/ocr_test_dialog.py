"""OCRテストダイアログ。

スクショを撮影 or ファイルから読み込み、マウスドラッグで範囲を選択して
Tesseract OCR で数値が読み取れるか確認する。
確認 OK なら region [x, y, w, h] を呼び出し元に返す。
"""
from __future__ import annotations

import os
from typing import Callable

import cv2
import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

LogFn = Callable[[str], None]

# Tesseract の有無を起動時に確認
try:
    import pytesseract
    _TESS_AVAILABLE = True
except ImportError:
    _TESS_AVAILABLE = False

_WHITELIST_OPTIONS = {
    "数字のみ (0-9)": "0123456789",
    "数字とスラッシュ (HP: 100/200)": "0123456789/",
    "数字とカンマ・ドット": "0123456789,.",
    "すべての文字": "",
}


# ------------------------------------------------------------------ キャンバス
class ImageCanvas(QWidget):
    """スクショを表示し、マウスドラッグで範囲 (QRect, 画像座標) を選択する。"""

    region_selected = Signal(int, int, int, int)   # x, y, w, h (画像座標)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(200, 150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.CrossCursor)

        self._pixmap: QPixmap | None = None
        self._img_w = 0
        self._img_h = 0
        self._scale = 1.0
        self._offset = QPoint(0, 0)

        self._drag_start: QPoint | None = None
        self._drag_rect: QRect | None = None     # ウィジェット座標
        self._selected_rect: QRect | None = None  # 画像座標

    def set_image(self, img: np.ndarray) -> None:
        """BGR numpy array をセットして表示。"""
        self._img_h, self._img_w = img.shape[:2]
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, self._img_w, self._img_h,
                      rgb.strides[0], QImage.Format_RGB888)
        self._pixmap = QPixmap.fromImage(qimg)
        self._drag_rect = None
        self._selected_rect = None
        self._update_scale()
        self.update()

    def _update_scale(self) -> None:
        if not self._pixmap:
            return
        sw = self.width() / self._img_w if self._img_w else 1.0
        sh = self.height() / self._img_h if self._img_h else 1.0
        self._scale = min(sw, sh, 1.0)  # 縮小のみ（拡大しない）
        dw = int(self._img_w * self._scale)
        dh = int(self._img_h * self._scale)
        self._offset = QPoint((self.width() - dw) // 2,
                               (self.height() - dh) // 2)

    def resizeEvent(self, event) -> None:
        self._update_scale()
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1e1e1e"))
        if self._pixmap:
            dw = int(self._img_w * self._scale)
            dh = int(self._img_h * self._scale)
            painter.drawPixmap(self._offset.x(), self._offset.y(), dw, dh, self._pixmap)

        if self._drag_rect and not self._drag_rect.isNull():
            pen = QPen(QColor("#ff6600"), 2, Qt.DashLine)
            painter.setPen(pen)
            painter.drawRect(self._drag_rect.normalized())

        if self._selected_rect:
            # 選択確定後は画像座標 → ウィジェット座標に変換して表示
            r = self._img_to_widget(self._selected_rect)
            pen = QPen(QColor("#00ff00"), 2, Qt.SolidLine)
            painter.setPen(pen)
            painter.drawRect(r)

    def _widget_to_img(self, p: QPoint) -> QPoint:
        if self._scale == 0:
            return p
        x = int((p.x() - self._offset.x()) / self._scale)
        y = int((p.y() - self._offset.y()) / self._scale)
        x = max(0, min(x, self._img_w - 1))
        y = max(0, min(y, self._img_h - 1))
        return QPoint(x, y)

    def _img_to_widget(self, r: QRect) -> QRect:
        x = int(r.x() * self._scale) + self._offset.x()
        y = int(r.y() * self._scale) + self._offset.y()
        w = int(r.width() * self._scale)
        h = int(r.height() * self._scale)
        return QRect(x, y, w, h)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._pixmap:
            self._drag_start = event.position().toPoint()
            self._drag_rect = QRect(self._drag_start, self._drag_start)
            self._selected_rect = None
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_start is not None:
            self._drag_rect = QRect(
                self._drag_start, event.position().toPoint()
            ).normalized()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._drag_start is not None:
            end = event.position().toPoint()
            rect_w = QRect(self._drag_start, end).normalized()
            if rect_w.width() > 4 and rect_w.height() > 4:
                tl = self._widget_to_img(rect_w.topLeft())
                br = self._widget_to_img(rect_w.bottomRight())
                self._selected_rect = QRect(tl, br).normalized()
                x = self._selected_rect.x()
                y = self._selected_rect.y()
                w = self._selected_rect.width()
                h = self._selected_rect.height()
                self.region_selected.emit(x, y, w, h)
            self._drag_start = None
            self._drag_rect = None
            self.update()

    def get_selected_region(self) -> tuple[int, int, int, int] | None:
        """選択中の領域を (x, y, w, h) で返す。未選択は None。"""
        if self._selected_rect and not self._selected_rect.isNull():
            r = self._selected_rect
            return (r.x(), r.y(), r.width(), r.height())
        return None

    def highlight_region(self, x: int, y: int, w: int, h: int) -> None:
        """外部から領域をハイライト（編集時の既存値表示用）。"""
        self._selected_rect = QRect(x, y, w, h)
        self.update()


# ------------------------------------------------------------------- ダイアログ
class OcrTestDialog(QDialog):
    """OCR テストダイアログ。

    呼び出し元は `exec()` == Accepted の後 `result_region()` で
    [x, y, w, h] を受け取る。
    """

    def __init__(self, serial: str | None = None,
                 initial_region: list[int] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OCR テスト — 範囲選択＆数値確認")
        self.setMinimumSize(900, 680)
        self._serial = serial
        self._img: np.ndarray | None = None
        self._result_region: list[int] | None = None

        self._build_ui()

        if initial_region and len(initial_region) == 4:
            self._apply_region_to_spinboxes(initial_region)

        if not _TESS_AVAILABLE:
            self._show_tess_warning()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # --- 上部: スクショ取得ボタン ---
        top = QHBoxLayout()
        btn_cap = QPushButton("📷 スクショ取得（接続中デバイス）")
        btn_cap.clicked.connect(self._capture)
        btn_file = QPushButton("📂 ファイルから開く")
        btn_file.clicked.connect(self._open_file)
        top.addWidget(btn_cap)
        top.addWidget(btn_file)
        top.addStretch()
        self._hint_label = QLabel("← まずスクショを取得し、数値が表示されている部分をドラッグで選択")
        self._hint_label.setStyleSheet("color: #777; font-size: 10px;")
        top.addWidget(self._hint_label)
        root.addLayout(top)

        # --- 中央: キャンバス ---
        self._canvas = ImageCanvas()
        self._canvas.region_selected.connect(self._on_region_selected)
        root.addWidget(self._canvas, 1)

        # --- 下部: 設定 + 結果 ---
        bottom = QHBoxLayout()

        # 範囲座標
        grp_region = QGroupBox("選択範囲（画像座標）")
        region_form = QFormLayout(grp_region)
        self._sx = QSpinBox(); self._sx.setRange(0, 9999); self._sx.setPrefix("x: ")
        self._sy = QSpinBox(); self._sy.setRange(0, 9999); self._sy.setPrefix("y: ")
        self._sw = QSpinBox(); self._sw.setRange(0, 9999); self._sw.setPrefix("w: ")
        self._sh = QSpinBox(); self._sh.setRange(0, 9999); self._sh.setPrefix("h: ")
        rh = QHBoxLayout()
        for sp in (self._sx, self._sy, self._sw, self._sh):
            rh.addWidget(sp)
        region_form.addRow("", rh)
        for sp in (self._sx, self._sy, self._sw, self._sh):
            sp.valueChanged.connect(self._on_spinbox_changed)
        bottom.addWidget(grp_region)

        # OCR 設定 + テスト
        grp_ocr = QGroupBox("OCR テスト")
        ocr_lay = QVBoxLayout(grp_ocr)

        wl_row = QHBoxLayout()
        wl_row.addWidget(QLabel("文字種:"))
        self._whitelist_combo = QComboBox()
        for label in _WHITELIST_OPTIONS:
            self._whitelist_combo.addItem(label)
        wl_row.addWidget(self._whitelist_combo)
        ocr_lay.addLayout(wl_row)

        btn_ocr = QPushButton("▶ OCR テスト実行")
        btn_ocr.clicked.connect(self._run_ocr)
        ocr_lay.addWidget(btn_ocr)

        self._ocr_result_label = QLabel("（テスト未実行）")
        self._ocr_result_label.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #1565c0; padding: 6px;"
        )
        self._ocr_result_label.setAlignment(Qt.AlignCenter)
        ocr_lay.addWidget(self._ocr_result_label)

        bottom.addWidget(grp_ocr)

        root.addLayout(bottom)

        # --- クロッププレビュー ---
        preview_row = QHBoxLayout()
        preview_row.addWidget(QLabel("切り抜き:"))
        self._crop_label = QLabel()
        self._crop_label.setMinimumSize(200, 60)
        self._crop_label.setMaximumHeight(100)
        self._crop_label.setStyleSheet("border: 1px solid #aaa; background: #000;")
        self._crop_label.setAlignment(Qt.AlignCenter)
        preview_row.addWidget(self._crop_label, 1)
        root.addLayout(preview_row)

        # --- ボタン ---
        btn_box = QHBoxLayout()
        self._btn_use = QPushButton("✓ この範囲をウォッチャーに設定")
        self._btn_use.setEnabled(False)
        self._btn_use.setStyleSheet(
            "QPushButton { background-color: #1565c0; color: white; font-weight: bold; padding: 6px; }"
            "QPushButton:hover { background-color: #0d47a1; }"
        )
        self._btn_use.clicked.connect(self._on_use)
        btn_cancel = QPushButton("閉じる")
        btn_cancel.clicked.connect(self.reject)
        btn_box.addWidget(self._btn_use)
        btn_box.addStretch()
        btn_box.addWidget(btn_cancel)
        root.addLayout(btn_box)

    # --------------------------------------------------------- スクショ取得
    def _capture(self) -> None:
        if not self._serial:
            QMessageBox.information(
                self, "情報",
                "デバイスが接続されていません。\n"
                "ランナータブでデバイスに接続してから実行してください。"
            )
            return
        try:
            from .adb import screencap
            self._hint_label.setText("スクショ取得中…")
            self.repaint()
            png = screencap(self._serial)
            arr = np.frombuffer(png, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("画像デコード失敗")
            self._img = img
            self._canvas.set_image(img)
            self._hint_label.setText("数値が表示されている部分をドラッグで選択してください")
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"スクショ取得失敗:\n{e}")

    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "画像ファイルを開く", "",
            "画像 (*.png *.jpg *.bmp *.jpeg)"
        )
        if not path:
            return
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            QMessageBox.critical(self, "エラー", f"画像を開けませんでした:\n{path}")
            return
        self._img = img
        self._canvas.set_image(img)
        self._hint_label.setText("数値が表示されている部分をドラッグで選択してください")

    # --------------------------------------------------------- 範囲選択
    def _on_region_selected(self, x: int, y: int, w: int, h: int) -> None:
        self._apply_region_to_spinboxes([x, y, w, h])
        self._update_crop_preview(x, y, w, h)
        self._btn_use.setEnabled(True)

    def _on_spinbox_changed(self) -> None:
        x, y, w, h = (self._sx.value(), self._sy.value(),
                      self._sw.value(), self._sh.value())
        if w > 0 and h > 0:
            self._canvas.highlight_region(x, y, w, h)
            self._update_crop_preview(x, y, w, h)
            self._btn_use.setEnabled(True)

    def _apply_region_to_spinboxes(self, region: list[int]) -> None:
        x, y, w, h = region
        for sp, v in zip((self._sx, self._sy, self._sw, self._sh), (x, y, w, h)):
            sp.blockSignals(True)
            sp.setValue(v)
            sp.blockSignals(False)
        if self._img is not None:
            self._canvas.highlight_region(x, y, w, h)
            self._update_crop_preview(x, y, w, h)
            self._btn_use.setEnabled(True)

    def _update_crop_preview(self, x: int, y: int, w: int, h: int) -> None:
        if self._img is None or w <= 0 or h <= 0:
            return
        ih, iw = self._img.shape[:2]
        x2 = min(x + w, iw)
        y2 = min(y + h, ih)
        x, y = max(0, x), max(0, y)
        crop = self._img[y:y2, x:x2]
        if crop.size == 0:
            return
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, rgb.shape[1], rgb.shape[0],
                      rgb.strides[0], QImage.Format_RGB888)
        pix = QPixmap.fromImage(qimg)
        self._crop_label.setPixmap(
            pix.scaled(self._crop_label.width(), self._crop_label.height(),
                       Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    # --------------------------------------------------------- OCR テスト
    def _run_ocr(self) -> None:
        if not _TESS_AVAILABLE:
            self._show_tess_warning()
            return
        x = self._sx.value(); y = self._sy.value()
        w = self._sw.value(); h = self._sh.value()
        if w <= 0 or h <= 0:
            QMessageBox.information(self, "情報", "範囲を選択してください")
            return
        if self._img is None:
            QMessageBox.information(self, "情報", "スクショを取得してください")
            return

        ih, iw = self._img.shape[:2]
        x2 = min(x + w, iw)
        y2 = min(y + h, ih)
        crop = self._img[max(0, y):y2, max(0, x):x2]
        if crop.size == 0:
            self._ocr_result_label.setText("（範囲が空）")
            return

        wl = _WHITELIST_OPTIONS[self._whitelist_combo.currentText()]
        config = "--psm 7 --oem 3"
        if wl:
            config += f" -c tessedit_char_whitelist={wl}"

        try:
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            # コントラスト強調（ゲームUIの細い数字に効果的）
            gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            _, gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            text = pytesseract.image_to_string(gray, config=config).strip()
            if not text:
                self._ocr_result_label.setText("読み取り結果: （空）— 範囲や文字種を変えてみてください")
                self._ocr_result_label.setStyleSheet(
                    "font-size: 14px; font-weight: bold; color: #c62828; padding: 6px;"
                )
            else:
                self._ocr_result_label.setText(f"読み取り結果: {text}")
                self._ocr_result_label.setStyleSheet(
                    "font-size: 18px; font-weight: bold; color: #1b5e20; padding: 6px;"
                )
        except Exception as e:
            self._ocr_result_label.setText(f"エラー: {e}")
            self._ocr_result_label.setStyleSheet(
                "font-size: 12px; color: #c62828; padding: 6px;"
            )

    def _show_tess_warning(self) -> None:
        QMessageBox.warning(
            self, "Tesseract 未インストール",
            "Tesseract OCR がインストールされていません。\n\n"
            "以下の手順でインストールしてください：\n\n"
            "1. https://github.com/UB-Mannheim/tesseract/wiki から\n"
            "   tesseract-ocr-w64-setup-x.x.exe をダウンロードしてインストール\n\n"
            "2. ターミナルで以下を実行:\n"
            "   pip install pytesseract\n\n"
            "3. インストール先（例）を環境変数 PATH に追加:\n"
            "   C:\\Program Files\\Tesseract-OCR"
        )

    # --------------------------------------------------------- 確定
    def _on_use(self) -> None:
        x = self._sx.value(); y = self._sy.value()
        w = self._sw.value(); h = self._sh.value()
        if w <= 0 or h <= 0:
            QMessageBox.information(self, "情報", "有効な範囲を選択してください")
            return
        self._result_region = [x, y, w, h]
        self.accept()

    def result_region(self) -> list[int] | None:
        """確定された [x, y, w, h]。未確定は None。"""
        return self._result_region
