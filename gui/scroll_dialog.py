"""自動スクロール（下→上）ステップの登録ダイアログ。

座標と時間にジッターを設定でき、再生時は毎回ランダムに揺らしてスワイプする。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QSpinBox, QVBoxLayout, QWidget,
)


def _hbox_widget(*widgets) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    for x in widgets:
        h.addWidget(x)
    return w


class ScrollDialog(QDialog):
    def __init__(self, logical_w: int, logical_h: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("自動スクロール（下→上）")
        self.resize(460, 340)

        if logical_w <= 0:
            logical_w = 2712
        if logical_h <= 0:
            logical_h = 1220
        cx = logical_w // 2
        y_bot = int(logical_h * 0.8)
        y_top = int(logical_h * 0.2)

        def spin(value: int, mn: int, mx: int, step: int = 1, suffix: str = "") -> QSpinBox:
            s = QSpinBox()
            s.setRange(mn, mx)
            s.setValue(value)
            s.setSingleStep(step)
            if suffix:
                s.setSuffix(suffix)
            return s

        self.x1 = spin(cx, 0, max(1, logical_w - 1))
        self.y1 = spin(y_bot, 0, max(1, logical_h - 1))
        self.x2 = spin(cx, 0, max(1, logical_w - 1))
        self.y2 = spin(y_top, 0, max(1, logical_h - 1))
        self.x1j = spin(30, 0, 500, suffix=" px")
        self.y1j = spin(30, 0, 500, suffix=" px")
        self.x2j = spin(30, 0, 500, suffix=" px")
        self.y2j = spin(30, 0, 500, suffix=" px")
        self.dur = spin(10000, 100, 60000, step=100, suffix=" ms")
        self.durj = spin(1500, 0, 30000, step=100, suffix=" ms")

        form = QFormLayout()
        form.addRow("開始 X:", _hbox_widget(self.x1, QLabel("±"), self.x1j))
        form.addRow("開始 Y:", _hbox_widget(self.y1, QLabel("±"), self.y1j))
        form.addRow("終了 X:", _hbox_widget(self.x2, QLabel("±"), self.x2j))
        form.addRow("終了 Y:", _hbox_widget(self.y2, QLabel("±"), self.y2j))
        form.addRow("時間   :", _hbox_widget(self.dur, QLabel("±"), self.durj))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "下から上へスワイプするスクロール動作を登録します。\n"
            "各値にジッター（±範囲）を設定すると毎回ランダムに揺らして実行します。"
        ))
        layout.addLayout(form)
        layout.addWidget(buttons)

    def get_params(self) -> dict:
        return {
            "x1": self.x1.value(), "y1": self.y1.value(),
            "x2": self.x2.value(), "y2": self.y2.value(),
            "x1_jitter": self.x1j.value(), "y1_jitter": self.y1j.value(),
            "x2_jitter": self.x2j.value(), "y2_jitter": self.y2j.value(),
            "duration_ms": self.dur.value(),
            "duration_jitter_ms": self.durj.value(),
        }
