"""バッテリー残量インジケータウィジェット。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize, QRect, QRectF, QPointF
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget


class BatteryIndicator(QWidget):
    """電池本体（残量バー）と % テキストを描画する小型ウィジェット。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._level: int | None = None
        self._charging: bool = False
        self.setFixedSize(QSize(86, 22))
        self.setToolTip("電池残量（接続後に更新）")

    def set_battery(self, level: int | None, charging: bool = False) -> None:
        self._level = None if level is None else max(0, min(100, int(level)))
        self._charging = bool(charging)
        if self._level is None:
            self.setToolTip("電池: 取得不可")
        else:
            self.setToolTip(
                f"電池: {self._level}%" + ("（充電中）" if self._charging else "")
            )
        self.update()

    @staticmethod
    def _level_color(level: int) -> QColor:
        if level >= 50:
            return QColor("#43A047")
        if level >= 20:
            return QColor("#FB8C00")
        return QColor("#E53935")

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        body_w, body_h = 32, 14
        nub_w, nub_h = 3, 6
        x0, y0 = 1, (self.height() - body_h) // 2
        body = QRectF(x0, y0, body_w, body_h)
        nub = QRectF(x0 + body_w, y0 + (body_h - nub_h) / 2, nub_w, nub_h)

        p.setPen(QPen(QColor("#444"), 1.2))
        p.setBrush(QColor("#fafafa"))
        p.drawRoundedRect(body, 2, 2)
        p.setBrush(QColor("#444"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(nub, 1, 1)

        if self._level is not None:
            inner = body.adjusted(2, 2, -2, -2)
            fill_w = inner.width() * (self._level / 100.0)
            fill = QRectF(inner.left(), inner.top(), fill_w, inner.height())
            p.setBrush(self._level_color(self._level))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(fill, 1, 1)
            if self._charging:
                cx = body.center().x()
                cy = body.center().y()
                bolt = QPolygonF([
                    QPointF(cx + 2, cy - 5),
                    QPointF(cx - 2, cy + 1),
                    QPointF(cx,     cy + 1),
                    QPointF(cx - 2, cy + 5),
                    QPointF(cx + 2, cy - 1),
                    QPointF(cx,     cy - 1),
                ])
                p.setPen(QPen(QColor("#222"), 0.8))
                p.setBrush(QColor("#FFEB3B"))
                p.drawPolygon(bolt)

        text = "—%" if self._level is None else f"{self._level}%"
        p.setPen(QColor("#222"))
        font = QFont(); font.setPointSize(9); font.setBold(True)
        p.setFont(font)
        text_rect = QRect(int(body.right() + nub_w + 4), 0,
                          self.width() - int(body.right() + nub_w + 4),
                          self.height())
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, text)
        p.end()
