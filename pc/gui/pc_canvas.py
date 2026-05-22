"""シーン編集用のスナップショット表示キャンバス。

役割:
- スナップショット PNG をアスペクト比保持で表示
- クリック / ドラッグの位置を **画像座標の 0.0〜1.0 正規化値** で通知
- タップマーカー・領域マーカーを重ね描き
- 右クリック保持 + マウスホイールで拡大/縮小
- 右ボタンドラッグで拡大時のパン

座標系:
- マウスイベント受信時、ピクセル座標 → 画像座標 → 正規化 (0.0〜1.0) に変換
- マーカーは正規化座標で持ち、毎描画時にピクセル座標へ展開
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget


@dataclass
class TapMarker:
    rx: float
    ry: float
    label: str = ""


@dataclass
class RegionMarker:
    rx: float
    ry: float
    rw: float
    rh: float
    label: str = ""


@dataclass
class CanvasState:
    taps:    list[TapMarker]    = field(default_factory=list)
    regions: list[RegionMarker] = field(default_factory=list)


class PcSnapshotCanvas(QWidget):
    """スナップショット表示 + クリック/ドラッグ検出。

    シグナル:
        clicked(rx, ry)                — 単発クリック（正規化座標）
        region_selected(rx, ry, rw, rh) — ドラッグで領域選択（正規化座標、左上 + 幅高）
    """

    clicked         = Signal(float, float)
    region_selected = Signal(float, float, float, float)

    DRAG_THRESHOLD_PX = 5   # この距離未満ならクリック扱い
    ZOOM_MIN = 1.0
    ZOOM_MAX = 10.0
    ZOOM_STEP = 1.2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setMouseTracking(False)
        self.setFocusPolicy(Qt.StrongFocus)

        self._pixmap: QPixmap | None = None
        self._image_rect = QRectF()  # 描画した画像の矩形（ピクセル座標）

        # 左ボタンドラッグ（クリック・領域選択用）
        self._drag_start: QPoint | None = None
        self._drag_current: QPoint | None = None

        # ズーム / パン
        self._zoom: float = 1.0
        self._pan: QPointF = QPointF(0.0, 0.0)   # 表示中心からのオフセット (px)

        # 右ボタン保持中のパン用
        self._right_pressed: bool = False
        self._right_drag_start: QPoint | None = None
        self._pan_at_right_start: QPointF = QPointF(0.0, 0.0)

        self.state = CanvasState()

    # ---------------------------------------------------------------- 公開 API
    def set_snapshot(self, path: str | None) -> bool:
        """スナップショット PNG をロードして表示する。"""
        if not path:
            self._pixmap = None
            self.update()
            return False
        pm = QPixmap(path)
        if pm.isNull():
            self._pixmap = None
            self.update()
            return False
        self._pixmap = pm
        self.update()
        return True

    def reset_zoom(self) -> None:
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def clear_markers(self) -> None:
        self.state = CanvasState()
        self.update()

    def set_markers(
        self,
        taps: list[TapMarker] | None = None,
        regions: list[RegionMarker] | None = None,
    ) -> None:
        if taps is not None:
            self.state.taps = list(taps)
        if regions is not None:
            self.state.regions = list(regions)
        self.update()

    # ---------------------------------------------------------------- 描画
    def paintEvent(self, e: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#222"))

        if self._pixmap is None or self._pixmap.isNull():
            p.setPen(QColor("#888"))
            p.drawText(self.rect(), Qt.AlignCenter, "（スナップショット未取得）")
            return

        # アスペクト比保持でフィットするサイズを計算
        ws = self.width()
        hs = self.height()
        pw = self._pixmap.width()
        ph = self._pixmap.height()
        if pw == 0 or ph == 0:
            return
        fit_scale = min(ws / pw, hs / ph)
        scale = fit_scale * self._zoom
        dw = pw * scale
        dh = ph * scale
        # 中央 + パンオフセット
        ox = (ws - dw) / 2.0 + self._pan.x()
        oy = (hs - dh) / 2.0 + self._pan.y()
        self._image_rect = QRectF(ox, oy, dw, dh)

        # クリッピングして widget の外に絵を描かない（オーバーラン抑制）
        p.save()
        p.setClipRect(self.rect())
        p.drawPixmap(self._image_rect, self._pixmap, QRectF(self._pixmap.rect()))
        p.restore()

        # マーカー
        p.setRenderHint(QPainter.Antialiasing)
        # タップマーカー
        pen_tap = QPen(QColor("#ffeb3b"))
        pen_tap.setWidth(2)
        p.setPen(pen_tap)
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        p.setFont(font)
        for m in self.state.taps:
            cx = ox + m.rx * dw
            cy = oy + m.ry * dh
            r = 7
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))
            p.drawLine(int(cx - r - 3), int(cy), int(cx + r + 3), int(cy))
            p.drawLine(int(cx), int(cy - r - 3), int(cx), int(cy + r + 3))
            if m.label:
                p.drawText(int(cx + r + 4), int(cy + 4), m.label)
        # 領域マーカー
        pen_rg = QPen(QColor("#4fc3f7"))
        pen_rg.setWidth(2)
        p.setPen(pen_rg)
        for r in self.state.regions:
            rx = ox + r.rx * dw
            ry = oy + r.ry * dh
            rw = r.rw * dw
            rh = r.rh * dh
            p.drawRect(QRectF(rx, ry, rw, rh))
            if r.label:
                p.drawText(int(rx + 2), int(ry - 2), r.label)

        # 現在のドラッグ
        if self._drag_start is not None and self._drag_current is not None:
            pen_d = QPen(QColor("#ff8a65"))
            pen_d.setWidth(2)
            pen_d.setStyle(Qt.DashLine)
            p.setPen(pen_d)
            r = QRect(self._drag_start, self._drag_current).normalized()
            p.drawRect(r)

    # ---------------------------------------------------------------- イベント
    def _to_norm(self, pos: QPoint) -> tuple[float, float] | None:
        """ピクセル位置を画像の正規化座標 (0.0〜1.0) へ変換。範囲外なら None。"""
        if self._image_rect.isEmpty():
            return None
        if not self._image_rect.contains(pos):
            return None
        rx = (pos.x() - self._image_rect.x()) / self._image_rect.width()
        ry = (pos.y() - self._image_rect.y()) / self._image_rect.height()
        return rx, ry

    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self._pixmap is None or self._pixmap.isNull():
            return
        if e.button() == Qt.RightButton:
            self._right_pressed = True
            self._right_drag_start = e.position().toPoint()
            self._pan_at_right_start = QPointF(self._pan)
            return
        if e.button() != Qt.LeftButton:
            return
        self._drag_start = e.position().toPoint()
        self._drag_current = self._drag_start
        self.update()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self._right_pressed and self._right_drag_start is not None:
            # 拡大時のみパン有効
            if self._zoom > 1.0:
                cur = e.position().toPoint()
                self._pan = QPointF(
                    self._pan_at_right_start.x() + (cur.x() - self._right_drag_start.x()),
                    self._pan_at_right_start.y() + (cur.y() - self._right_drag_start.y()),
                )
                self.update()
            return
        if self._drag_start is None:
            return
        self._drag_current = e.position().toPoint()
        self.update()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if e.button() == Qt.RightButton:
            self._right_pressed = False
            self._right_drag_start = None
            return
        if self._drag_start is None or e.button() != Qt.LeftButton:
            return
        start = self._drag_start
        end = e.position().toPoint()
        self._drag_start = None
        self._drag_current = None

        dx = end.x() - start.x()
        dy = end.y() - start.y()
        is_click = (dx * dx + dy * dy) < (self.DRAG_THRESHOLD_PX ** 2)

        if is_click:
            norm = self._to_norm(end)
            if norm is not None:
                self.clicked.emit(norm[0], norm[1])
        else:
            # 矩形の左上 / 右下を画像座標で求める
            n1 = self._to_norm(start)
            n2 = self._to_norm(end)
            if n1 is None or n2 is None:
                self.update()
                return
            rx = min(n1[0], n2[0])
            ry = min(n1[1], n2[1])
            rw = abs(n2[0] - n1[0])
            rh = abs(n2[1] - n1[1])
            # 極小領域は無視
            if rw * rh > 1e-6:
                self.region_selected.emit(rx, ry, rw, rh)
        self.update()

    def wheelEvent(self, e: QWheelEvent) -> None:  # noqa: N802
        """右ボタン保持中のホイールでズーム。マウス位置を中心に拡大する。"""
        if not self._right_pressed:
            super().wheelEvent(e)
            return
        delta = e.angleDelta().y()
        if delta == 0:
            return
        factor = self.ZOOM_STEP if delta > 0 else 1.0 / self.ZOOM_STEP
        new_zoom = max(self.ZOOM_MIN, min(self.ZOOM_MAX, self._zoom * factor))
        if new_zoom == self._zoom:
            e.accept()
            return
        # マウス位置を画像内のどの点で押さえているかを保ち拡大する
        pos = e.position()
        before_x = pos.x() - self._image_rect.x()
        before_y = pos.y() - self._image_rect.y()
        ratio = new_zoom / self._zoom
        # 拡大後の同じ画像座標を保つようにパンを補正
        self._pan = QPointF(
            self._pan.x() - before_x * (ratio - 1),
            self._pan.y() - before_y * (ratio - 1),
        )
        self._zoom = new_zoom
        if self._zoom <= 1.0:
            self._pan = QPointF(0.0, 0.0)
        e.accept()
        self.update()
