"""コンパクトな経験値オーバーレイ。常に手前・枠なし・ドラッグ移動可。

- 左クリック: ドラッグで移動
- 右クリック: メニュー（設定復帰 / 一時停止再開 / 終了）
- ダブルクリック: 設定ウィンドウへ復帰
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QAction, QCursor, QMouseEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QPushButton, QWidget

from .exp_meter import ExpMeter


class OverlayWindow(QWidget):
    """常に手前に出る小さなステータスバー。"""

    request_setup       = Signal()  # 設定ウィンドウを開く要求
    request_toggle      = Signal()  # 計測の一時停止/再開
    request_reset       = Signal()  # サンプル・累積値をリセット
    request_quit        = Signal()  # アプリ終了

    def __init__(self, meter: ExpMeter) -> None:
        super().__init__()
        self._meter = meter
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setStyleSheet(
            "QWidget { background: rgba(20,20,20,220); color: #eee; "
            "border-radius: 8px; }"
            "QLabel { font-size: 12px; padding: 0 6px; }"
            "QLabel#cur { color: #ffd54f; font-weight: bold; font-size: 13px; }"
            "QPushButton { background: rgba(255,255,255,30); color: #eee; "
            "border: 1px solid rgba(255,255,255,60); border-radius: 4px; "
            "padding: 1px 6px; font-size: 12px; }"
            "QPushButton:hover { background: rgba(255,255,255,60); }"
        )

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(2)

        # 各ラベルは固定幅にしてテキスト変動でウィンドウ幅が変わらないようにする
        self._lbl_cur   = QLabel("—"); self._lbl_cur.setObjectName("cur")
        self._lbl_spd   = QLabel("—")
        self._lbl_eta   = QLabel("—")
        self._lbl_st    = QLabel("")
        self._lbl_cur.setFixedWidth(105)
        self._lbl_spd.setFixedWidth(110)
        self._lbl_eta.setFixedWidth(95)
        self._lbl_st.setFixedWidth(20)   # ⚠ アイコン用、長文は省略
        self._lbl_st.setAlignment(Qt.AlignCenter)
        for w in (self._lbl_cur, self._lbl_spd, self._lbl_eta, self._lbl_st):
            lay.addWidget(w)

        # 操作ボタン群
        lay.addSpacing(4)

        self._btn_toggle = QPushButton("■")
        self._btn_toggle.setToolTip("計測の開始/停止")
        self._btn_toggle.setCursor(Qt.PointingHandCursor)
        self._btn_toggle.clicked.connect(self.request_toggle.emit)
        lay.addWidget(self._btn_toggle)

        self._btn_reset = QPushButton("🔄")
        self._btn_reset.setToolTip("サンプル・累積値をリセット")
        self._btn_reset.setCursor(Qt.PointingHandCursor)
        self._btn_reset.clicked.connect(self.request_reset.emit)
        lay.addWidget(self._btn_reset)

        self._btn_setup = QPushButton("⚙")
        self._btn_setup.setToolTip("設定ウィンドウを開く")
        self._btn_setup.setCursor(Qt.PointingHandCursor)
        self._btn_setup.clicked.connect(self.request_setup.emit)
        lay.addWidget(self._btn_setup)

        self._btn_close = QPushButton("✕")
        self._btn_close.setToolTip("終了")
        self._btn_close.setCursor(Qt.PointingHandCursor)
        self._btn_close.clicked.connect(self.request_quit.emit)
        lay.addWidget(self._btn_close)

        self._drag_offset: QPoint | None = None
        meter.updated.connect(self.refresh)
        meter.status_changed.connect(self._on_status)
        self.refresh()
        # 初回レイアウト後にサイズを固定（以後はテキストが変わってもウィンドウ幅は不変）
        self.adjustSize()
        self.setFixedSize(self.size())

    # ---------------------------------------------------------------- 描画
    def refresh(self) -> None:
        m = self._meter
        if m.prev_raw is not None:
            self._lbl_cur.setText(f"{m.prev_raw:.4f}%")
        else:
            self._lbl_cur.setText("—")

        cur = m.current_speed()
        avg = m.avg_speed()
        spd = cur if cur is not None else avg
        if spd is not None:
            arrow = "↗" if cur is not None and avg is not None and cur > avg else (
                    "↘" if cur is not None and avg is not None and cur < avg else "→")
            self._lbl_spd.setText(f"{arrow} {spd:.1f}%/h")
        else:
            self._lbl_spd.setText("速度: —")

        eta_cur, eta_avg = m.eta_to_levelup()
        eta_min = eta_cur if eta_cur is not None else eta_avg
        if eta_min is not None:
            if eta_min >= 60:
                h, mn = divmod(int(eta_min), 60)
                self._lbl_eta.setText(f"残 {h}h{mn:02d}m")
            else:
                self._lbl_eta.setText(f"残 {int(eta_min)}分")
        else:
            self._lbl_eta.setText("残 —")

        # 開始/停止トグルの見た目を反映
        self._btn_toggle.setText("■" if m.running else "▶")
        self._btn_toggle.setToolTip("停止" if m.running else "開始")

    def _on_status(self, s: str) -> None:
        # ⚠ アイコンのみ表示、詳細はツールチップに
        if s.startswith("⚠"):
            self._lbl_st.setText("⚠")
            self._lbl_st.setToolTip(s)
        else:
            self._lbl_st.setText("")
            self._lbl_st.setToolTip("")

    # ---------------------------------------------------------------- 入力
    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None

    def mouseDoubleClickEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self.request_setup.emit()

    def contextMenuEvent(self, e) -> None:  # noqa: N802
        menu = QMenu(self)
        a_setup  = QAction("⚙ 設定を開く", menu)
        a_toggle = QAction("⏸ 一時停止" if self._meter.running else "▶ 計測再開", menu)
        a_quit   = QAction("✕ 終了", menu)
        a_setup.triggered.connect(self.request_setup.emit)
        a_toggle.triggered.connect(self.request_toggle.emit)
        a_quit.triggered.connect(self.request_quit.emit)
        menu.addAction(a_setup)
        menu.addAction(a_toggle)
        menu.addSeparator()
        menu.addAction(a_quit)
        menu.exec(QCursor.pos())
