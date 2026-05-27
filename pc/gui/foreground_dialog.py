"""シーン開始前に対象ウィンドウが前面でない時の確認ダイアログ。

3 つの選択肢:
    - run:  即時実施（前面化してシーン実行）
    - wait: 3 分待機後に再評価
    - skip: このシーン実行をスキップ

30 秒以内に選択がなければ「人がいない」と判断し、自動的に "run" を選択する。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
    QWidget,
)


# 自動選択までのタイムアウト（秒）
_AUTO_TIMEOUT_S = 30

# 「待機」を選んだ場合のスリープ秒数（PcFlowRunner 側で参照）
WAIT_SLEEP_SECONDS = 180   # 3 分


class ForegroundConfirmDialog(QDialog):
    """対象ウィンドウが前面でない時に出すタイムアウト付き確認ダイアログ。

    `choice` 属性に最終的な選択 ("run" / "wait" / "skip") が入る。
    タイムアウト時は "run"（人がいないとみなして自動進行）。
    """

    def __init__(
        self,
        scene_name: str,
        timeout_s: int = _AUTO_TIMEOUT_S,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("対象ウィンドウが前面にありません")
        self.setModal(True)
        self.setMinimumWidth(440)
        # 最前面表示 + 自動的にこのダイアログ自体は前面化を試みる
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowStaysOnTopHint
        )

        self.choice: str = "run"   # 既定 (タイムアウト時もこれ)
        self._remaining = timeout_s

        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        msg = QLabel(
            f"<b>{scene_name}</b> の実行を開始しようとしていますが、<br>"
            f"対象ウィンドウが前面にありません。"
        )
        msg.setWordWrap(True)
        msg.setStyleSheet("font-size:13px;")
        lay.addWidget(msg)

        self._lbl_count = QLabel()
        self._lbl_count.setAlignment(Qt.AlignCenter)
        self._lbl_count.setStyleSheet(
            "color:#1565c0; font-size:12px; font-weight:bold; "
            "padding:6px; background:#e3f2fd; border-radius:4px;"
        )
        lay.addWidget(self._lbl_count)

        # 3 ボタン横並び
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._btn_run = QPushButton("▶ 即時実施")
        self._btn_run.setToolTip("前面化してすぐにシーンを実行します")
        self._btn_run.setStyleSheet(
            "background:#1976d2; color:white; font-weight:bold; padding:8px;"
        )
        self._btn_run.clicked.connect(lambda: self._finish("run"))
        btn_row.addWidget(self._btn_run, 1)

        self._btn_wait = QPushButton("⏸ 3 分待機")
        self._btn_wait.setToolTip("3 分後に再評価します（席を離れる時に）")
        self._btn_wait.setStyleSheet("padding:8px;")
        self._btn_wait.clicked.connect(lambda: self._finish("wait"))
        btn_row.addWidget(self._btn_wait, 1)

        self._btn_skip = QPushButton("⊘ スキップ")
        self._btn_skip.setToolTip("このシーン実行を中止します")
        self._btn_skip.setStyleSheet("padding:8px;")
        self._btn_skip.clicked.connect(lambda: self._finish("skip"))
        btn_row.addWidget(self._btn_skip, 1)

        lay.addLayout(btn_row)

        # カウントダウンタイマー
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()
        self._update_count_label()

    def _update_count_label(self) -> None:
        self._lbl_count.setText(
            f"⏱  あと {self._remaining} 秒 で自動的に「即時実施」されます"
        )

    def _on_tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self._finish("run")
            return
        self._update_count_label()

    def _finish(self, choice: str) -> None:
        self.choice = choice
        self._timer.stop()
        self.accept()
