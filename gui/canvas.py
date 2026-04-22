"""スナップショット表示キャンバス。

- クリック/ドラッグで座標・矩形を通知
- 番号付きのタップマーカーを重ね描画できる
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, QPointF, QRect, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel


Marker = tuple[int, int, int, bool]  # (番号, logical x, logical y, ハイライト)


class SnapshotCanvas(QLabel):
    clicked = Signal(int, int)
    region_selected = Signal(int, int, int, int)

    def __init__(self) -> None:
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(400, 300)
        self.setStyleSheet("background-color: #202020; color: #ffffff;")
        self.setText("スナップショット未取得")

        self._orig_pixmap: QPixmap | None = None
        self._scaled_pixmap: QPixmap | None = None
        self._img_w = 0
        self._img_h = 0
        self._display_rect = QRect()
        self._scale = 1.0
        self._drag_start: QPoint | None = None
        self._drag_end: QPoint | None = None
        self._markers: list[Marker] = []

    def set_snapshot(self, pixmap: QPixmap | None) -> None:
        self._orig_pixmap = pixmap
        if pixmap:
            self._img_w = pixmap.width()
            self._img_h = pixmap.height()
            self.setText("")
        else:
            self._img_w = 0
            self._img_h = 0
            self.setText("スナップショット未取得")
        self._update_scaled()
        self.update()

    def current_pixmap(self) -> QPixmap | None:
        return self._orig_pixmap

    def set_markers(self, markers: list[Marker]) -> None:
        self._markers = markers
        self.update()

    # ---------------------------------------------------------------- paint
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_scaled()

    def _update_scaled(self) -> None:
        if not self._orig_pixmap or self._img_w == 0 or self._img_h == 0:
            self._scaled_pixmap = None
            self._display_rect = QRect()
            self._scale = 1.0
            return
        w, h = max(1, self.width()), max(1, self.height())
        scale = min(w / self._img_w, h / self._img_h)
        disp_w = max(1, int(self._img_w * scale))
        disp_h = max(1, int(self._img_h * scale))
        self._scaled_pixmap = self._orig_pixmap.scaled(
            disp_w, disp_h, Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        off_x = (w - disp_w) // 2
        off_y = (h - disp_h) // 2
        self._display_rect = QRect(off_x, off_y, disp_w, disp_h)
        self._scale = scale

    def paintEvent(self, event):
        if not self._scaled_pixmap:
            super().paintEvent(event)
            return
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.black)
        p.drawPixmap(self._display_rect.topLeft(), self._scaled_pixmap)

        for num, lx, ly, hi in self._markers:
            wx = self._display_rect.left() + lx * self._scale
            wy = self._display_rect.top() + ly * self._scale
            radius = 22 if hi else 14
            fill = QColor("#FFD600") if hi else QColor("#E53935")
            p.setBrush(fill)
            p.setPen(QPen(QColor("white"), 2))
            p.drawEllipse(QPointF(wx, wy), radius, radius)
            font = QFont()
            font.setBold(True)
            font.setPointSize(12 if hi else 9)
            p.setFont(font)
            p.setPen(QColor("black"))
            rect = QRect(int(wx - radius), int(wy - radius),
                         int(radius * 2), int(radius * 2))
            p.drawText(rect, Qt.AlignCenter, str(num))

        if self._drag_start and self._drag_end:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(Qt.red, 2, Qt.SolidLine))
            p.drawRect(QRect(self._drag_start, self._drag_end).normalized())

    # ----------------------------------------------------------------- mouse
    def _widget_to_logical(self, pt: QPoint) -> tuple[int, int] | None:
        if not self._display_rect.contains(pt) or self._scale <= 0:
            return None
        lx = int((pt.x() - self._display_rect.left()) / self._scale)
        ly = int((pt.y() - self._display_rect.top()) / self._scale)
        return lx, ly

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_start = event.position().toPoint()
            self._drag_end = self._drag_start

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_start is not None:
            self._drag_end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._drag_start is not None and self._drag_end is not None:
            s = self._widget_to_logical(self._drag_start)
            e = self._widget_to_logical(self._drag_end)
            if s and e:
                if abs(e[0] - s[0]) < 10 and abs(e[1] - s[1]) < 10:
                    self.clicked.emit(s[0], s[1])
                else:
                    x = min(s[0], e[0]); y = min(s[1], e[1])
                    w = abs(e[0] - s[0]); h = abs(e[1] - s[1])
                    self.region_selected.emit(x, y, w, h)
        self._drag_start = None
        self._drag_end = None
        self.update()
