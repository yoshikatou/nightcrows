"""スナップショット表示キャンバス。

- クリック/ドラッグで座標・矩形を通知
- 番号付きのタップマーカーを重ね描画できる
- マーカーをドラッグして座標を移動できる
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, QPointF, QRect, QRectF, Signal
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

        # ドラッグステップ用の始点・終点ピン (logical coords)
        self._drag_pin_start: tuple[int, int] | None = None
        self._drag_pin_end: tuple[int, int] | None = None

        # wait_image マッチテスト用オーバーレイ
        self._match_region: list[int] | None = None    # 検索範囲 [x, y, w, h]
        self._match_rect: list[int] | None = None      # マッチ位置 [x, y, w, h]
        self._match_score: float | None = None
        self._match_threshold: float = 0.85

        # if_image ブランチタップマーカー
        self._branch_then_markers: list[tuple[int, int]] = []  # (lx, ly)
        self._branch_else_markers: list[tuple[int, int]] = []  # (lx, ly)

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

    def set_drag_pins(self, start: tuple[int, int] | None, end: tuple[int, int] | None = None) -> None:
        self._drag_pin_start = start
        self._drag_pin_end = end
        self.update()

    def set_match_overlay(self, region: list[int] | None, match_rect: list[int] | None,
                          score: float | None, threshold: float = 0.85) -> None:
        self._match_region = region
        self._match_rect = match_rect
        self._match_score = score
        self._match_threshold = threshold
        self.update()

    def clear_match_overlay(self) -> None:
        self._match_region = None
        self._match_rect = None
        self._match_score = None
        self.update()

    def set_branch_markers(self,
                           then_taps: list[tuple[int, int]],
                           else_taps: list[tuple[int, int]]) -> None:
        self._branch_then_markers = list(then_taps)
        self._branch_else_markers = list(else_taps)
        self.update()

    def clear_branch_markers(self) -> None:
        self._branch_then_markers = []
        self._branch_else_markers = []
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

        # if_image ブランチタップマーカー
        for branch_markers, fill, label_prefix in [
            (self._branch_then_markers, QColor("#1B5E20"), "✓"),
            (self._branch_else_markers, QColor("#B71C1C"), "✗"),
        ]:
            for idx, (lx, ly) in enumerate(branch_markers):
                wx = self._display_rect.left() + lx * self._scale
                wy = self._display_rect.top() + ly * self._scale
                r = 18
                p.setBrush(fill)
                p.setPen(QPen(QColor("white"), 2))
                p.drawEllipse(QPointF(wx, wy), r, r)
                font_b = QFont(); font_b.setBold(True); font_b.setPointSize(9)
                p.setFont(font_b)
                p.setPen(QColor("white"))
                p.drawText(
                    QRect(int(wx - r), int(wy - r), r * 2, r * 2),
                    Qt.AlignCenter, f"{label_prefix}{idx + 1}"
                )

        if self._drag_start and self._drag_end:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(Qt.red, 2, Qt.SolidLine))
            p.drawRect(QRect(self._drag_start, self._drag_end).normalized())

        # ドラッグステップ始点ピン（青）・終点ピン（緑）
        for pin_coord, color, label in [
            (self._drag_pin_start, QColor("#1565C0"), "S"),
            (self._drag_pin_end,   QColor("#2E7D32"), "E"),
        ]:
            if pin_coord is None:
                continue
            plx, ply = pin_coord
            pwx = self._display_rect.left() + plx * self._scale
            pwy = self._display_rect.top()  + ply * self._scale
            r = 16
            p.setBrush(color)
            p.setPen(QPen(QColor("white"), 2))
            p.drawEllipse(QPointF(pwx, pwy), r, r)
            font2 = QFont()
            font2.setBold(True)
            font2.setPointSize(9)
            p.setFont(font2)
            p.setPen(QColor("white"))
            p.drawText(QRect(int(pwx - r), int(pwy - r), r * 2, r * 2), Qt.AlignCenter, label)
        # 始点と終点が両方あれば矢印線を描画
        if self._drag_pin_start and self._drag_pin_end:
            sx = self._display_rect.left() + self._drag_pin_start[0] * self._scale
            sy = self._display_rect.top()  + self._drag_pin_start[1] * self._scale
            ex = self._display_rect.left() + self._drag_pin_end[0]   * self._scale
            ey = self._display_rect.top()  + self._drag_pin_end[1]   * self._scale
            p.setPen(QPen(QColor("#FFA000"), 2, Qt.DashLine))
            p.drawLine(QPointF(sx, sy), QPointF(ex, ey))

        # wait_image マッチテストオーバーレイ
        def _lrect(lx, ly, lw, lh) -> QRectF:
            return QRectF(
                self._display_rect.left() + lx * self._scale,
                self._display_rect.top()  + ly * self._scale,
                lw * self._scale, lh * self._scale,
            )
        if self._match_region:
            rx, ry, rw, rh = self._match_region
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#1565C0"), 2, Qt.DashLine))
            p.drawRect(_lrect(rx, ry, rw, rh))
        if self._match_rect is not None and self._match_score is not None:
            mx, my, mw, mh = self._match_rect
            ok = self._match_score >= self._match_threshold
            hit_color = QColor("#2E7D32") if ok else QColor("#C62828")
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(hit_color, 3))
            r = _lrect(mx, my, mw, mh)
            p.drawRect(r)
            lbl = f"{'✓' if ok else '✗'} {self._match_score:.3f}"
            score_font = QFont(); score_font.setBold(True); score_font.setPointSize(11)
            p.setFont(score_font)
            p.setPen(QPen(QColor("black"), 1))
            p.drawText(QPointF(r.x() + 2, r.y() - 6), lbl)
            p.setPen(hit_color)
            p.drawText(QPointF(r.x() + 1, r.y() - 7), lbl)

    # ----------------------------------------------------------------- mouse
    def _widget_to_logical(self, pt: QPoint) -> tuple[int, int] | None:
        if self._scale <= 0 or self._display_rect.isEmpty():
            return None
        lx = int((pt.x() - self._display_rect.left()) / self._scale)
        ly = int((pt.y() - self._display_rect.top()) / self._scale)
        lx = max(0, min(lx, self._img_w - 1))
        ly = max(0, min(ly, self._img_h - 1))
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
