"""キャプチャ画像からドラッグで領域を選択し、ウィンドウサイズ比率で保存する。

操作:
  左ドラッグ : 範囲選択
  ホイール   : ズーム（カーソル中心）
  右ドラッグ : 画像をパン
  ズームリセットボタン : 1.0 倍に戻す

領域は (rel_x, rel_y, rel_w, rel_h) の浮動小数（0.0〜1.0）として返す。
"""
from __future__ import annotations

import cv2
import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .capture import capture_window
from .window_picker import find_hwnd_by_title


class _ImageCanvas(QWidget):
    """画像表示＋ホイールズーム＋右ドラッグパン＋左ドラッグ範囲選択。"""

    region_selected = Signal(int, int, int, int)  # x, y, w, h (image px)

    _MIN_ZOOM = 0.5
    _MAX_ZOOM = 10.0

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(400, 300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)

        self._pixmap: QPixmap | None = None
        self._img_w = 0
        self._img_h = 0
        self._base_scale = 1.0
        self._zoom = 1.0
        self._offset = QPoint(0, 0)
        self._pan = QPoint(0, 0)

        self._drag_start: QPoint | None = None
        self._drag_rect: QRect | None = None
        self._selected_rect: QRect | None = None  # image coords

        self._pan_start: QPoint | None = None
        self._pan_start_saved: QPoint | None = None

    # ---------------------------------------------------------------- 画像
    def set_image(self, img: np.ndarray) -> None:
        self._img_h, self._img_w = img.shape[:2]
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, self._img_w, self._img_h,
                      rgb.strides[0], QImage.Format_RGB888)
        self._pixmap = QPixmap.fromImage(qimg.copy())
        self._drag_rect = None
        self._selected_rect = None
        self.reset_zoom()

    def reset_zoom(self) -> None:
        self._zoom = 1.0
        self._pan = QPoint(0, 0)
        self._update_base_scale()
        self.update()

    def _update_base_scale(self) -> None:
        if not self._pixmap or self._img_w == 0 or self._img_h == 0:
            return
        sw = self.width()  / self._img_w
        sh = self.height() / self._img_h
        self._base_scale = min(sw, sh, 1.0)
        dw = int(self._img_w * self._base_scale)
        dh = int(self._img_h * self._base_scale)
        self._offset = QPoint((self.width()  - dw) // 2,
                              (self.height() - dh) // 2)

    def _total_scale(self) -> float:
        return self._base_scale * self._zoom

    def resizeEvent(self, e) -> None:  # noqa: N802
        self._update_base_scale()
        super().resizeEvent(e)

    # ---------------------------------------------------------------- 描画
    def paintEvent(self, e) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#1e1e1e"))
        if self._pixmap:
            ts = self._total_scale()
            dw = int(self._img_w * ts)
            dh = int(self._img_h * ts)
            ox = self._offset.x() + self._pan.x()
            oy = self._offset.y() + self._pan.y()
            p.drawPixmap(ox, oy, dw, dh, self._pixmap)

        if self._drag_rect and not self._drag_rect.isNull():
            p.setPen(QPen(QColor("#ff6600"), 2, Qt.DashLine))
            p.drawRect(self._drag_rect.normalized())

        if self._selected_rect:
            r = self._img_to_widget(self._selected_rect)
            p.setPen(QPen(QColor("#00ff00"), 2, Qt.SolidLine))
            p.drawRect(r)

        if self._pixmap:
            p.setPen(QColor("#ffcc00"))
            p.drawText(
                self.rect().adjusted(4, 4, -4, -4),
                Qt.AlignBottom | Qt.AlignRight,
                f"× {self._zoom:.1f}   ホイール:ズーム  右ドラッグ:移動"
            )

    # ---------------------------------------------------------------- 座標変換
    def _widget_to_img(self, p: QPoint) -> QPoint:
        ts = self._total_scale()
        if ts == 0:
            return p
        x = int((p.x() - self._offset.x() - self._pan.x()) / ts)
        y = int((p.y() - self._offset.y() - self._pan.y()) / ts)
        x = max(0, min(x, self._img_w - 1))
        y = max(0, min(y, self._img_h - 1))
        return QPoint(x, y)

    def _img_to_widget(self, r: QRect) -> QRect:
        ts = self._total_scale()
        x = int(r.x() * ts) + self._offset.x() + self._pan.x()
        y = int(r.y() * ts) + self._offset.y() + self._pan.y()
        w = int(r.width()  * ts)
        h = int(r.height() * ts)
        return QRect(x, y, w, h)

    def _clamp_pan(self) -> None:
        if not self._pixmap:
            return
        ts = self._total_scale()
        dw = int(self._img_w * ts)
        dh = int(self._img_h * ts)
        margin = 40
        px = max(-(dw - margin), min(self.width()  - margin, self._pan.x()))
        py = max(-(dh - margin), min(self.height() - margin, self._pan.y()))
        self._pan = QPoint(px, py)

    # ---------------------------------------------------------------- マウス
    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self._pixmap:
            return
        pos = event.position().toPoint()
        delta = event.angleDelta().y()
        factor = 1.15 if delta > 0 else 1 / 1.15
        new_zoom = max(self._MIN_ZOOM, min(self._MAX_ZOOM, self._zoom * factor))

        ts = self._total_scale()
        img_x = (pos.x() - self._offset.x() - self._pan.x()) / ts if ts else 0
        img_y = (pos.y() - self._offset.y() - self._pan.y()) / ts if ts else 0

        self._zoom = new_zoom
        new_ts = self._total_scale()
        self._pan = QPoint(
            int(pos.x() - self._offset.x() - img_x * new_ts),
            int(pos.y() - self._offset.y() - img_y * new_ts),
        )
        self._clamp_pan()
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.RightButton and self._pixmap:
            self._pan_start = event.position().toPoint()
            self._pan_start_saved = QPoint(self._pan)
            self.setCursor(Qt.ClosedHandCursor)
        elif event.button() == Qt.LeftButton and self._pixmap:
            self._drag_start = event.position().toPoint()
            self._drag_rect = QRect(self._drag_start, self._drag_start)
            self._selected_rect = None
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._pan_start is not None:
            delta = event.position().toPoint() - self._pan_start
            self._pan = self._pan_start_saved + delta
            self._clamp_pan()
            self.update()
        elif self._drag_start is not None:
            self._drag_rect = QRect(
                self._drag_start, event.position().toPoint()
            ).normalized()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.RightButton:
            self._pan_start = None
            self._pan_start_saved = None
            self.setCursor(Qt.CrossCursor)
        elif event.button() == Qt.LeftButton and self._drag_start is not None:
            end = event.position().toPoint()
            rect_w = QRect(self._drag_start, end).normalized()
            if rect_w.width() > 4 and rect_w.height() > 4:
                tl = self._widget_to_img(rect_w.topLeft())
                br = self._widget_to_img(rect_w.bottomRight())
                self._selected_rect = QRect(tl, br).normalized()
                r = self._selected_rect
                self.region_selected.emit(r.x(), r.y(), r.width(), r.height())
            self._drag_start = None
            self._drag_rect = None
            self.update()

    def highlight_region(self, x: int, y: int, w: int, h: int) -> None:
        self._selected_rect = QRect(x, y, w, h)
        self.update()


class RegionPickerDialog(QDialog):
    """指定ウィンドウをキャプチャ→ドラッグで領域選択→比率で返す。"""

    def __init__(
        self,
        window_title: str,
        current_rel: list[float] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("経験値%の表示領域を設定")
        self.setMinimumSize(900, 600)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)
        self._window_title = window_title
        self._rel: list[float] = list(current_rel) if current_rel else []
        self._img_size: tuple[int, int] = (0, 0)
        self._build_ui()
        if self._window_title:
            self._capture()

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        hint = QLabel(
            "ゲームウィンドウのスクショを取得して、経験値%が表示されている領域をドラッグで囲んでください。\n"
            "ホイールでズーム / 右ドラッグでパン / 領域はウィンドウサイズに対する比率で保存されます。"
        )
        hint.setStyleSheet("color:#555; font-size:11px;")
        lay.addWidget(hint)

        row = QHBoxLayout()
        btn_cap  = QPushButton("📷 スクショ取得")
        btn_zoom = QPushButton("🔍 ズームリセット")
        btn_cap.clicked.connect(self._capture)
        btn_zoom.clicked.connect(lambda: self._canvas.reset_zoom())
        row.addWidget(btn_cap)
        row.addWidget(btn_zoom)
        row.addStretch()
        lay.addLayout(row)

        self._canvas = _ImageCanvas()
        self._canvas.region_selected.connect(self._on_region)
        lay.addWidget(self._canvas, 1)

        self._lbl = QLabel(self._format_rel())
        lay.addWidget(self._lbl)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _format_rel(self) -> str:
        if not self._rel:
            return "選択範囲: （未選択）"
        return (
            f"選択範囲(比率): x={self._rel[0]:.4f} y={self._rel[1]:.4f} "
            f"w={self._rel[2]:.4f} h={self._rel[3]:.4f}"
        )

    def _capture(self) -> None:
        hwnd = find_hwnd_by_title(self._window_title)
        if not hwnd:
            self._canvas.setToolTip(f"ウィンドウが見つかりません: {self._window_title}")
            return
        img = capture_window(hwnd)
        if img is None:
            self._canvas.setToolTip("キャプチャに失敗しました（最小化中の可能性）")
            return
        h, w = img.shape[:2]
        self._img_size = (w, h)
        self._canvas.set_image(img)
        if self._rel and w > 0 and h > 0:
            rx = int(self._rel[0] * w)
            ry = int(self._rel[1] * h)
            rw = int(self._rel[2] * w)
            rh = int(self._rel[3] * h)
            self._canvas.highlight_region(rx, ry, rw, rh)

    def _on_region(self, x: int, y: int, w: int, h: int) -> None:
        iw, ih = self._img_size
        if iw <= 0 or ih <= 0:
            return
        self._rel = [x / iw, y / ih, w / iw, h / ih]
        self._lbl.setText(self._format_rel())

    def get_rel(self) -> list[float]:
        return self._rel
