"""スナップショット表示キャンバス。

- クリック/ドラッグで座標・矩形を通知
- 番号付きのタップマーカーを重ね描画できる
- マーカーをドラッグして座標を移動できる
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, QPointF, QRect, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel


Marker = tuple[int, int, int, bool]  # (番号, logical x, logical y, ハイライト)

_HIT_RADIUS = 20  # widget px — マーカーのドラッグ開始判定距離


class SnapshotCanvas(QLabel):
    clicked = Signal(int, int)
    region_selected = Signal(int, int, int, int)
    marker_moved = Signal(int, int, int)  # marker_index, new_lx, new_ly
    right_clicked = Signal(int, int)      # logical x, y

    def __init__(self) -> None:
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(400, 300)
        self.setStyleSheet("background-color: #202020; color: #ffffff;")
        self.setText("スナップショット未取得")
        self.setMouseTracking(True)

        self._orig_pixmap: QPixmap | None = None
        self._scaled_pixmap: QPixmap | None = None
        self._img_w = 0
        self._img_h = 0
        self._display_rect = QRect()
        self._scale = 1.0
        self._drag_start: QPoint | None = None
        self._drag_end: QPoint | None = None
        self._markers: list[Marker] = []

        self._dragging_marker_idx: int | None = None
        self._drag_marker_widget_pos: QPoint | None = None

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

        for i, (num, lx, ly, hi) in enumerate(self._markers):
            if i == self._dragging_marker_idx:
                continue  # ドラッグ中は下で別描画
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

        # ドラッグ中マーカーを緑でプレビュー表示
        if self._dragging_marker_idx is not None and self._drag_marker_widget_pos is not None:
            num, _lx, _ly, _hi = self._markers[self._dragging_marker_idx]
            wx = float(self._drag_marker_widget_pos.x())
            wy = float(self._drag_marker_widget_pos.y())
            radius = 22
            p.setBrush(QColor("#4CAF50"))
            p.setPen(QPen(QColor("white"), 2))
            p.drawEllipse(QPointF(wx, wy), radius, radius)
            font = QFont()
            font.setBold(True)
            font.setPointSize(12)
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

    def _find_marker_at(self, pt: QPoint) -> int | None:
        for i, (num, lx, ly, hi) in enumerate(self._markers):
            wx = self._display_rect.left() + lx * self._scale
            wy = self._display_rect.top() + ly * self._scale
            dist2 = (pt.x() - wx) ** 2 + (pt.y() - wy) ** 2
            if dist2 <= _HIT_RADIUS ** 2:
                return i
        return None

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.RightButton:
            logical = self._widget_to_logical(event.position().toPoint())
            if logical:
                self.right_clicked.emit(logical[0], logical[1])
            return
        if event.button() == Qt.LeftButton:
            pt = event.position().toPoint()
            idx = self._find_marker_at(pt)
            if idx is not None:
                self._dragging_marker_idx = idx
                self._drag_marker_widget_pos = pt
            else:
                self._drag_start = pt
                self._drag_end = pt

    def mouseMoveEvent(self, event: QMouseEvent):
        pt = event.position().toPoint()
        if self._dragging_marker_idx is not None:
            self._drag_marker_widget_pos = pt
            self.update()
        elif self._drag_start is not None:
            self._drag_end = pt
            self.update()
        else:
            # ホバー時のカーソル変更
            if self._find_marker_at(pt) is not None:
                self.setCursor(Qt.SizeAllCursor)
            else:
                self.setCursor(Qt.ArrowCursor)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._dragging_marker_idx is not None:
            if self._drag_marker_widget_pos is not None:
                logical = self._widget_to_logical(self._drag_marker_widget_pos)
                if logical:
                    self.marker_moved.emit(self._dragging_marker_idx, logical[0], logical[1])
            self._dragging_marker_idx = None
            self._drag_marker_widget_pos = None
            self.update()
        elif self._drag_start is not None and self._drag_end is not None:
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
