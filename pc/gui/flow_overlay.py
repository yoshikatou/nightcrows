"""フロー実行中のステータスを半透明オーバーレイで表示するウィンドウ。

経験値メーターの OverlayWindow と同じ作り（FramelessWindowHint +
WindowStaysOnTopHint）。PcFlowRunner のシグナルを受けて表示を更新する。

表示内容:
    - 1 行目: 実行中シーン名 + ステップ進捗
    - 2 行目: 次回スケジュール (next_schedule_changed)
    - 3 行目: 直近のウォッチャー発火 (任意。発生時のみ表示)

操作:
    - 左ドラッグで移動（離した位置を Signal で通知 → settings に保存）
    - 右クリックメニュー: 非表示 / フロー停止
    - ⏸ ボタンでフロー停止要求 (Signal を通知)
    - ✕ ボタンで非表示 (フローは止めない)
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QAction, QCursor, QMouseEvent
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMenu, QPushButton, QVBoxLayout, QWidget,
)


class FlowOverlay(QWidget):
    """フロー実行中のステータスを表示する常時前面・枠なしの半透明バー。"""

    request_stop_flow = Signal()    # ⏸ ボタンや右クリックメニューから停止要求
    request_hide      = Signal()    # ✕ ボタンや右クリックメニューから非表示要求
    moved             = Signal(int, int)   # ドラッグ終了位置 (x, y) 通知

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
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
            "QLabel#scene { color: #ffd54f; font-weight: bold; font-size: 13px; }"
            "QLabel#next  { color: #90caf9; font-size: 11px; }"
            "QLabel#fire  { color: #ff8a65; font-size: 11px; }"
            "QPushButton { background: rgba(255,255,255,30); color: #eee; "
            "border: 1px solid rgba(255,255,255,60); border-radius: 4px; "
            "padding: 1px 6px; font-size: 12px; }"
            "QPushButton:hover { background: rgba(255,255,255,60); }"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(2)

        # 1 行目: シーン名 + ステップ進捗 + 操作ボタン
        row1 = QHBoxLayout()
        row1.setSpacing(4)
        self._lbl_scene = QLabel("待機中")
        self._lbl_scene.setObjectName("scene")
        row1.addWidget(self._lbl_scene, 1)

        self._btn_stop = QPushButton("⏸")
        self._btn_stop.setToolTip("フロー停止")
        self._btn_stop.setCursor(Qt.PointingHandCursor)
        self._btn_stop.clicked.connect(self.request_stop_flow.emit)
        row1.addWidget(self._btn_stop)

        self._btn_close = QPushButton("✕")
        self._btn_close.setToolTip("オーバーレイを閉じる (フローは継続)")
        self._btn_close.setCursor(Qt.PointingHandCursor)
        self._btn_close.clicked.connect(self.request_hide.emit)
        row1.addWidget(self._btn_close)

        outer.addLayout(row1)

        # 2 行目: 次回スケジュール
        self._lbl_next = QLabel("")
        self._lbl_next.setObjectName("next")
        outer.addWidget(self._lbl_next)

        # 3 行目: ウォッチャー発火（出たり消えたり）
        self._lbl_fire = QLabel("")
        self._lbl_fire.setObjectName("fire")
        self._lbl_fire.hide()
        outer.addWidget(self._lbl_fire)

        self._drag_offset: QPoint | None = None
        self.setMinimumWidth(320)

    # ------------------------------------------------ 状態更新スロット
    def update_scene(self, name: str, step: int, total: int) -> None:
        if total > 0:
            self._lbl_scene.setText(f"▶ {name}  {step}/{total}")
        else:
            self._lbl_scene.setText(f"▶ {name}")

    def update_step(self, step: int, total: int, scene_name: str = "") -> None:
        cur = self._lbl_scene.text()
        # シーン名はそのまま、ステップ部分だけ差し替える
        if cur.startswith("▶"):
            head = cur.split("  ")[0]
            self._lbl_scene.setText(f"{head}  {step}/{total}")
        else:
            self._lbl_scene.setText(f"▶ {scene_name or '実行中'}  {step}/{total}")

    def update_next_schedule(self, text: str) -> None:
        self._lbl_next.setText(text)

    def update_state(self, state: str) -> None:
        """state = "idle" | "running"。idle なら表示を初期化。"""
        if state == "idle":
            self._lbl_scene.setText("待機中")
            self._lbl_fire.hide()
            self._lbl_fire.setText("")

    def show_watcher_fired(
        self, watcher_id: str, title: str, today_count: int, last_time: str,
    ) -> None:
        """ウォッチャー発火の最新情報を表示。"""
        self._lbl_fire.setText(
            f"🔥 {title}  本日 {today_count} 回  最終 {last_time}"
        )
        self._lbl_fire.show()

    # ------------------------------------------------ ドラッグ移動
    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._drag_offset = (
                e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self._drag_offset = None
            pos = self.pos()
            self.moved.emit(pos.x(), pos.y())

    def contextMenuEvent(self, e) -> None:  # noqa: N802
        menu = QMenu(self)
        a_stop = QAction("⏸ フロー停止", menu)
        a_stop.triggered.connect(self.request_stop_flow.emit)
        menu.addAction(a_stop)
        a_hide = QAction("✕ オーバーレイを閉じる", menu)
        a_hide.triggered.connect(self.request_hide.emit)
        menu.addAction(a_hide)
        menu.exec(QCursor.pos())
