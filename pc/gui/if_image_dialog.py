"""画像で分岐 (if_image) ステップの編集ダイアログ。

各分岐 (成立時 / 不成立時) は次のいずれか:
    - インライン手順: PcStep のリストをこのシーン内に直接埋め込む（再帰実行）
    - シーン呼び出し: scenes/ にある別シーンを呼ぶ（従来形式）

ダイアログ閉時に dict として `{"then" or "then_scene": ...}` / `{"else" or "else_scene": ...}`
の組合せを返す。インライン手順が空でない場合はそちらを優先（バックエンドの解釈と一致）。

サポートするインライン手順: tap / wait_fixed / keyevent / call_scene
（位置指定の必要な tap_image, wait_image, swipe 等はキャンバスでの作成が必要なので
本ダイアログでは扱わない）。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMenu, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)


def _step_label(step: dict) -> str:
    """インライン手順 1 件を 1 行で表示する文字列を返す。"""
    if not isinstance(step, dict):
        return "(不正な手順)"
    t = step.get("type", "?")
    if t == "tap":
        rx = float(step.get("rx", 0.5))
        ry = float(step.get("ry", 0.5))
        btn = step.get("button", "L")
        return f"タップ rx={rx:.3f} ry={ry:.3f} [{btn}]"
    if t == "wait_fixed":
        return f"待機 {float(step.get('seconds', 1.0)):.1f} 秒"
    if t == "keyevent":
        return f"キー入力 {step.get('key', '')!r} {int(step.get('duration_ms', 30))}ms"
    if t == "call_scene":
        return f"シーン呼出 {step.get('scene', '')}"
    if t == "group_header":
        return f"━ {step.get('label', '')} ━"
    return f"{t}  {step}"


class _BranchPanel(QWidget):
    """成立時 / 不成立時 1 分岐のパネル（インライン手順 or シーン呼出を切替）。"""

    def __init__(
        self,
        title: str,
        initial_inline: list[dict] | None,
        initial_scene_name: str,
        available_scenes: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._inline_steps: list[dict] = [dict(s) for s in (initial_inline or [])]
        self._available_scenes = available_scenes

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        group = QGroupBox(title)
        outer = QVBoxLayout(group)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        # モード選択ラジオ
        mode_row = QHBoxLayout()
        self._rb_inline = QRadioButton("インライン手順")
        self._rb_scene  = QRadioButton("シーン呼出")
        self._rb_none   = QRadioButton("何もしない")
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._rb_inline)
        self._mode_group.addButton(self._rb_scene)
        self._mode_group.addButton(self._rb_none)
        mode_row.addWidget(self._rb_inline)
        mode_row.addWidget(self._rb_scene)
        mode_row.addWidget(self._rb_none)
        mode_row.addStretch(1)
        outer.addLayout(mode_row)

        # インライン手順リスト
        self._list = QListWidget()
        self._list.setStyleSheet("font-family: Consolas, monospace; font-size:11px;")
        self._list.setMinimumHeight(110)
        outer.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        self._btn_add  = QPushButton("+ 追加 ▾")
        self._btn_add.clicked.connect(self._show_add_menu)
        self._btn_edit = QPushButton("編集")
        self._btn_edit.clicked.connect(self._edit_selected)
        self._btn_up   = QPushButton("↑")
        self._btn_up.clicked.connect(lambda: self._move_selected(-1))
        self._btn_dn   = QPushButton("↓")
        self._btn_dn.clicked.connect(lambda: self._move_selected(+1))
        self._btn_del  = QPushButton("削除")
        self._btn_del.clicked.connect(self._remove_selected)
        for b in (self._btn_add, self._btn_edit, self._btn_up, self._btn_dn, self._btn_del):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        # シーン選択 combo
        scene_row = QHBoxLayout()
        scene_row.addWidget(QLabel("シーン:"))
        self._combo_scene = QComboBox()
        self._combo_scene.addItem("(なし)")
        for s in available_scenes:
            self._combo_scene.addItem(s)
        if initial_scene_name and initial_scene_name in available_scenes:
            self._combo_scene.setCurrentText(initial_scene_name)
        scene_row.addWidget(self._combo_scene, 1)
        outer.addLayout(scene_row)

        lay.addWidget(group)

        # 初期モード判定
        if self._inline_steps:
            self._rb_inline.setChecked(True)
        elif initial_scene_name:
            self._rb_scene.setChecked(True)
        else:
            self._rb_none.setChecked(True)
        self._mode_group.buttonClicked.connect(self._refresh_enabled)

        self._refresh_list()
        self._refresh_enabled()

    # ---- 公開: 編集結果を取り出す
    def collect(self) -> dict:
        """ダイアログ閉時に呼び出して、このブランチの保存用キー値を返す。

        戻り値の dict は if_image の params に merge できる形（インラインモードなら
        {"<branch>": [...], "<branch>_scene": ""}、シーンモードなら逆）。
        呼び出し側で branch_key ("then" / "else") を当てて使う。
        """
        if self._rb_inline.isChecked():
            return {"inline": list(self._inline_steps), "scene": ""}
        if self._rb_scene.isChecked():
            sel = self._combo_scene.currentText()
            return {"inline": [], "scene": "" if sel == "(なし)" else sel}
        return {"inline": [], "scene": ""}

    # ---- 内部
    def _refresh_enabled(self) -> None:
        inline = self._rb_inline.isChecked()
        scene  = self._rb_scene.isChecked()
        for b in (self._list, self._btn_add, self._btn_edit,
                  self._btn_up, self._btn_dn, self._btn_del):
            b.setEnabled(inline)
        self._combo_scene.setEnabled(scene)

    def _refresh_list(self) -> None:
        self._list.clear()
        for s in self._inline_steps:
            item = QListWidgetItem(_step_label(s))
            self._list.addItem(item)

    def _show_add_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("タップ (rx, ry 直接入力)", self._add_tap)
        menu.addAction("待機 (秒)",                self._add_wait_fixed)
        menu.addAction("キー入力",                 self._add_keyevent)
        if self._available_scenes:
            menu.addAction("シーン呼出",            self._add_call_scene)
        menu.exec(QCursor.pos())

    def _add_tap(self) -> None:
        rx, ok = QInputDialog.getDouble(
            self, "タップ", "rx (0.0〜1.0):", 0.5, 0.0, 1.0, 4,
        )
        if not ok:
            return
        ry, ok = QInputDialog.getDouble(
            self, "タップ", "ry (0.0〜1.0):", 0.5, 0.0, 1.0, 4,
        )
        if not ok:
            return
        self._inline_steps.append(
            {"type": "tap", "rx": round(rx, 4), "ry": round(ry, 4), "duration_ms": 50}
        )
        self._refresh_list()
        self._list.setCurrentRow(len(self._inline_steps) - 1)

    def _add_wait_fixed(self) -> None:
        v, ok = QInputDialog.getDouble(
            self, "待機", "待機秒数:", 1.0, 0.1, 600.0, 1,
        )
        if not ok:
            return
        self._inline_steps.append({"type": "wait_fixed", "seconds": v})
        self._refresh_list()
        self._list.setCurrentRow(len(self._inline_steps) - 1)

    def _add_keyevent(self) -> None:
        key, ok = QInputDialog.getText(
            self, "キー入力", "キー名 (例: esc, enter, f5, a, space):",
        )
        if not ok:
            return
        key = key.strip()
        if not key:
            return
        dur, ok = QInputDialog.getInt(
            self, "キー入力", "押下時間 (ms):", 30, 1, 5000, 10,
        )
        if not ok:
            return
        self._inline_steps.append(
            {"type": "keyevent", "key": key, "duration_ms": dur}
        )
        self._refresh_list()
        self._list.setCurrentRow(len(self._inline_steps) - 1)

    def _add_call_scene(self) -> None:
        if not self._available_scenes:
            return
        item, ok = QInputDialog.getItem(
            self, "シーン呼出", "呼び出すシーン:",
            self._available_scenes, 0, False,
        )
        if not ok:
            return
        self._inline_steps.append({"type": "call_scene", "scene": item})
        self._refresh_list()
        self._list.setCurrentRow(len(self._inline_steps) - 1)

    def _edit_selected(self) -> None:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._inline_steps):
            return
        step = self._inline_steps[row]
        t = step.get("type")
        if t == "tap":
            rx, ok = QInputDialog.getDouble(
                self, "タップ", "rx (0.0〜1.0):",
                float(step.get("rx", 0.5)), 0.0, 1.0, 4,
            )
            if not ok:
                return
            ry, ok = QInputDialog.getDouble(
                self, "タップ", "ry (0.0〜1.0):",
                float(step.get("ry", 0.5)), 0.0, 1.0, 4,
            )
            if not ok:
                return
            step["rx"] = round(rx, 4)
            step["ry"] = round(ry, 4)
        elif t == "wait_fixed":
            v, ok = QInputDialog.getDouble(
                self, "待機", "待機秒数:",
                float(step.get("seconds", 1.0)), 0.1, 600.0, 1,
            )
            if not ok:
                return
            step["seconds"] = v
        elif t == "keyevent":
            key, ok = QInputDialog.getText(
                self, "キー入力", "キー名:",
                text=str(step.get("key", "")),
            )
            if not ok:
                return
            dur, ok = QInputDialog.getInt(
                self, "キー入力", "押下時間 (ms):",
                int(step.get("duration_ms", 30)), 1, 5000, 10,
            )
            if not ok:
                return
            step["key"] = key.strip()
            step["duration_ms"] = dur
        elif t == "call_scene":
            if not self._available_scenes:
                return
            cur = str(step.get("scene", ""))
            idx = self._available_scenes.index(cur) if cur in self._available_scenes else 0
            item, ok = QInputDialog.getItem(
                self, "シーン呼出", "呼び出すシーン:",
                self._available_scenes, idx, False,
            )
            if not ok:
                return
            step["scene"] = item
        else:
            return
        self._refresh_list()
        self._list.setCurrentRow(row)

    def _move_selected(self, direction: int) -> None:
        row = self._list.currentRow()
        if row < 0:
            return
        new_row = row + direction
        if new_row < 0 or new_row >= len(self._inline_steps):
            return
        self._inline_steps[row], self._inline_steps[new_row] = (
            self._inline_steps[new_row], self._inline_steps[row]
        )
        self._refresh_list()
        self._list.setCurrentRow(new_row)

    def _remove_selected(self) -> None:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._inline_steps):
            return
        del self._inline_steps[row]
        self._refresh_list()
        if self._inline_steps:
            self._list.setCurrentRow(min(row, len(self._inline_steps) - 1))


class IfImageEditDialog(QDialog):
    """if_image ステップの then / else を編集するダイアログ。

    使い方:
        dlg = IfImageEditDialog(step.params, list_scenes_callback, parent)
        if dlg.exec() == QDialog.Accepted:
            new_params = dlg.collect()
            step.params.update(new_params)
    """

    def __init__(
        self,
        params: dict,
        list_scenes: Callable[[], list[str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("画像で分岐 — 編集")
        self.setMinimumWidth(520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # ヘッダー: テンプレ情報（表示のみ）
        tpl   = str(params.get("template", params.get("path", "")))
        thr   = float(params.get("threshold", 0.85))
        region = params.get("region")
        info = QLabel(
            f"テンプレ: <code>{tpl}</code><br>"
            f"閾値: {thr:.2f}    領域: "
            f"{tuple(round(v, 3) for v in region) if region else '(全体)'}"
        )
        info.setTextFormat(Qt.RichText)
        info.setWordWrap(True)
        info.setStyleSheet("color:#444; font-size:11px;")
        outer.addWidget(info)

        scenes = list_scenes() or []

        self._then = _BranchPanel(
            "成立時 (画像あり)",
            params.get("then"),
            str(params.get("then_scene", "")),
            scenes,
            parent=self,
        )
        self._else = _BranchPanel(
            "不成立時 (画像なし)",
            params.get("else"),
            str(params.get("else_scene", "")),
            scenes,
            parent=self,
        )
        outer.addWidget(self._then)
        outer.addWidget(self._else)

        hint = QLabel(
            "インライン手順は同じ if_image ステップ内に保存されます。"
            "外部シーンを呼ぶ場合は「シーン呼出」を選択。"
        )
        hint.setStyleSheet("color:#666; font-size:11px;")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    def collect(self) -> dict:
        """OK で抜けた後、if_image の params に merge する更新 dict を返す。

        インラインモードなら "<branch>" にリストを、"<branch>_scene" は空文字。
        シーンモードならその逆。「何もしない」は両方空。
        """
        t = self._then.collect()
        e = self._else.collect()
        return {
            "then":       t["inline"],
            "then_scene": t["scene"],
            "else":       e["inline"],
            "else_scene": e["scene"],
        }
