"""GUI 全体で使う汎用ウィジェット。"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QListWidget


class ReorderableListWidget(QListWidget):
    """ドラッグで行並べ替え可能、完了時に rows_reordered を発火する QListWidget。

    QListWidget の InternalMove では rowsMoved が
    （remove + insert に分解されて）発火しないことがあるため、
    dropEvent をフックして並べ替え完了を確実に拾う。
    """

    rows_reordered = Signal()

    def dropEvent(self, e) -> None:  # noqa: N802
        super().dropEvent(e)
        self.rows_reordered.emit()
