"""画像系ステップ (`tap_image` / `wait_image` / `if_image`) の値編集ダイアログ。

編集できる項目:
    - threshold (一致閾値 0.0〜1.0)
    - timeout_s (tap_image / wait_image のみ)
    - region (探索領域 X/Y/W/H 比率、または「領域なし」で全体)
    - tap_offset_x / tap_offset_y (tap_image のみ、クリック位置の微調整)

「探したい画像」(テンプレート) のプレビューと、「今のゲーム画面でテスト」ボタンで
現在の設定でマッチするかを即確認できる。テンプレート画像自体は変更しない
（差し替えはキャンバスでドラッグする従来フロー）。
"""
from __future__ import annotations

import os
from datetime import datetime

import cv2
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from .capture_clean import capture_window_clean
from .region_picker import RegionPickerDialog
from .window_picker import find_hwnd_by_title


_PREVIEW_MAX_W = 240
_PREVIEW_MAX_H = 80
_TEST_PREVIEW_MAX_W = 380
_TEST_PREVIEW_MAX_H = 220


def _bgr_to_qpixmap(bgr: np.ndarray, max_w: int, max_h: int) -> QPixmap:
    """OpenCV BGR ndarray を QPixmap に変換し、最大サイズに収まるようスケール。"""
    h, w = bgr.shape[:2]
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    rgb = np.ascontiguousarray(rgb)
    qimg = QImage(rgb.data, w, h, w * 3, QImage.Format_RGB888).copy()
    pix = QPixmap.fromImage(qimg)
    if pix.width() > max_w or pix.height() > max_h:
        pix = pix.scaled(
            max_w, max_h,
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
    return pix


class ImageStepEditDialog(QDialog):
    """tap_image / wait_image / if_image の値編集ダイアログ。

    使い方:
        dlg = ImageStepEditDialog(step.type, step.params, window_title, self)
        if dlg.exec() == QDialog.Accepted:
            updates = dlg.collect()
            if updates.get("region") is None:
                step.params.pop("region", None)
                updates.pop("region", None)
            step.params.update(updates)
    """

    def __init__(
        self,
        step_type: str,
        params: dict,
        window_title: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._step_type = step_type
        self._window_title = window_title
        self._template_path = str(
            params.get("template", params.get("path", ""))
        )
        self.setWindowTitle(f"値編集 ({step_type})")
        self.setMinimumWidth(480)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # ===== 探したい画像 (テンプレート) プレビュー =====
        tpl_grp = QGroupBox("探したい画像 (テンプレート)")
        tlay = QVBoxLayout(tpl_grp)
        tlay.setContentsMargins(8, 6, 8, 6)

        path_row = QHBoxLayout()
        self._lbl_tpl_path = QLabel(self._template_path)
        self._lbl_tpl_path.setStyleSheet("color:#555; font-size:11px;")
        self._lbl_tpl_path.setWordWrap(True)
        self._lbl_tpl_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path_row.addWidget(self._lbl_tpl_path, 1)
        self._btn_change_tpl = QPushButton("テンプレを変更…")
        self._btn_change_tpl.setToolTip(
            "現在のゲーム画面をキャプチャして、ドラッグで新しいテンプレ領域を選択。"
            "新しい画像が templates/<シーン名>/ に保存され、このステップが参照する"
            "テンプレが差し替わります。"
        )
        self._btn_change_tpl.clicked.connect(self._change_template)
        path_row.addWidget(self._btn_change_tpl)
        tlay.addLayout(path_row)

        preview_row = QHBoxLayout()
        self._lbl_tpl_preview = QLabel()
        self._lbl_tpl_preview.setFrameShape(QFrame.Box)
        self._lbl_tpl_preview.setStyleSheet("background:#eee;")
        self._lbl_tpl_preview.setMinimumSize(40, 30)
        self._lbl_tpl_preview.setMaximumSize(_PREVIEW_MAX_W, _PREVIEW_MAX_H)
        self._lbl_tpl_size = QLabel("—")
        self._lbl_tpl_size.setStyleSheet("color:#555; font-size:11px;")
        preview_row.addWidget(self._lbl_tpl_preview)
        preview_row.addWidget(self._lbl_tpl_size, 1)
        tlay.addLayout(preview_row)

        self._load_template_preview()
        outer.addWidget(tpl_grp)

        # ===== 検出条件 =====
        cond_grp = QGroupBox("検出条件")
        cform = QFormLayout(cond_grp)
        cform.setLabelAlignment(Qt.AlignRight)

        self._spin_threshold = QDoubleSpinBox()
        self._spin_threshold.setRange(0.0, 1.0)
        self._spin_threshold.setSingleStep(0.05)
        self._spin_threshold.setDecimals(2)
        self._spin_threshold.setValue(float(params.get("threshold", 0.85)))
        self._spin_threshold.setToolTip(
            "一致を認める最低 score (0.0〜1.0)。\n"
            "背景がアニメ/ノイズで揺れるなら 0.70〜0.75 を試す。"
        )
        cform.addRow("一致閾値:", self._spin_threshold)

        if step_type in ("tap_image", "wait_image"):
            self._spin_timeout: QDoubleSpinBox | None = QDoubleSpinBox()
            self._spin_timeout.setRange(0.1, 600.0)
            self._spin_timeout.setSingleStep(1.0)
            self._spin_timeout.setDecimals(1)
            self._spin_timeout.setSuffix(" 秒")
            self._spin_timeout.setValue(float(params.get("timeout_s", 10.0)))
            cform.addRow("タイムアウト:", self._spin_timeout)
        else:
            self._spin_timeout = None

        outer.addWidget(cond_grp)

        # ===== 探す場所 (探索領域) =====
        region_grp = QGroupBox("探す場所 (ゲーム画面のクライアント比率 0.0〜1.0)")
        rlay = QFormLayout(region_grp)
        rlay.setLabelAlignment(Qt.AlignRight)
        region = params.get("region")
        has_region = bool(region) and len(region) == 4

        def _mk_spin(initial: float) -> QDoubleSpinBox:
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 1.0)
            sp.setSingleStep(0.005)
            sp.setDecimals(4)
            sp.setValue(float(initial))
            return sp

        rx = float(region[0]) if has_region else 0.0
        ry = float(region[1]) if has_region else 0.0
        rw = float(region[2]) if has_region else 1.0
        rh = float(region[3]) if has_region else 1.0

        self._spin_rx = _mk_spin(rx)
        self._spin_ry = _mk_spin(ry)
        self._spin_rw = _mk_spin(rw)
        self._spin_rh = _mk_spin(rh)

        rlay.addRow("X (左上):", self._spin_rx)
        rlay.addRow("Y (左上):", self._spin_ry)
        rlay.addRow("幅 (W):",   self._spin_rw)
        rlay.addRow("高さ (H):", self._spin_rh)

        self._chk_no_region = QCheckBox("領域なし (画面全体を探索)")
        self._chk_no_region.setChecked(not has_region)
        self._chk_no_region.toggled.connect(self._on_no_region_toggled)
        rlay.addRow("", self._chk_no_region)

        self._btn_pick_region = QPushButton("範囲を画像で選択…")
        self._btn_pick_region.setToolTip(
            "現在のゲーム画面をキャプチャして、ドラッグで探す範囲を選択。"
            "X/Y/幅/高さに自動入力します。"
        )
        self._btn_pick_region.clicked.connect(self._pick_region)
        rlay.addRow("", self._btn_pick_region)

        hint = QLabel(
            "ヒント: 探す場所はテンプレ画像より「明確に大きい」必要があります。"
            "同サイズだと 1 箇所しか試行できず、わずかなズレで失敗します。"
            "テンプレの 2〜4 倍が目安。"
        )
        hint.setStyleSheet("color:#666; font-size:11px;")
        hint.setWordWrap(True)
        rlay.addRow("", hint)

        outer.addWidget(region_grp)

        # ===== テスト =====
        test_grp = QGroupBox("テスト: 現在のゲーム画面で試す")
        tlay2 = QVBoxLayout(test_grp)
        tlay2.setContentsMargins(8, 6, 8, 6)

        test_btn_row = QHBoxLayout()
        self._btn_test = QPushButton("▶ 今のゲーム画面でテスト")
        self._btn_test.clicked.connect(self._run_test)
        test_btn_row.addWidget(self._btn_test)
        test_btn_row.addStretch(1)
        tlay2.addLayout(test_btn_row)

        self._lbl_test_result = QLabel("(未実行)")
        self._lbl_test_result.setStyleSheet("color:#555; font-size:11px;")
        self._lbl_test_result.setWordWrap(True)
        tlay2.addWidget(self._lbl_test_result)

        self._lbl_test_preview = QLabel()
        self._lbl_test_preview.setFrameShape(QFrame.Box)
        self._lbl_test_preview.setStyleSheet("background:#eee;")
        self._lbl_test_preview.setMinimumHeight(40)
        self._lbl_test_preview.setMaximumSize(
            _TEST_PREVIEW_MAX_W, _TEST_PREVIEW_MAX_H
        )
        self._lbl_test_preview.setAlignment(Qt.AlignCenter)
        tlay2.addWidget(self._lbl_test_preview)

        outer.addWidget(test_grp)

        # ===== tap_image のオフセット =====
        if step_type == "tap_image":
            off_grp = QGroupBox("クリック位置オフセット (px)")
            olay = QFormLayout(off_grp)
            olay.setLabelAlignment(Qt.AlignRight)
            self._spin_off_x: QSpinBox | None = QSpinBox()
            self._spin_off_x.setRange(-500, 500)
            self._spin_off_x.setValue(int(params.get("tap_offset_x", 0)))
            self._spin_off_y: QSpinBox | None = QSpinBox()
            self._spin_off_y.setRange(-500, 500)
            self._spin_off_y.setValue(int(params.get("tap_offset_y", 0)))
            olay.addRow("X:", self._spin_off_x)
            olay.addRow("Y:", self._spin_off_y)
            off_hint = QLabel(
                "テンプレ中心からの相対 px。例: テキストの右にあるボタンを"
                "押したいときに +X、下のボタンなら +Y。"
            )
            off_hint.setStyleSheet("color:#666; font-size:11px;")
            off_hint.setWordWrap(True)
            olay.addRow("", off_hint)
            outer.addWidget(off_grp)
        else:
            self._spin_off_x = None
            self._spin_off_y = None

        # ===== OK / Cancel =====
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

        self._on_no_region_toggled(self._chk_no_region.isChecked())

    # ---------------------------------------------------------------- preview
    def _load_template_preview(self) -> None:
        if not self._template_path or not os.path.isfile(self._template_path):
            self._lbl_tpl_preview.setText("画像なし")
            self._lbl_tpl_size.setText("テンプレファイルが見つかりません")
            return
        img = cv2.imread(self._template_path, cv2.IMREAD_COLOR)
        if img is None:
            self._lbl_tpl_preview.setText("読込失敗")
            self._lbl_tpl_size.setText("(画像を読み込めません)")
            return
        pix = _bgr_to_qpixmap(img, _PREVIEW_MAX_W, _PREVIEW_MAX_H)
        self._lbl_tpl_preview.setPixmap(pix)
        h, w = img.shape[:2]
        self._lbl_tpl_size.setText(
            f"サイズ: {w} × {h} px\n"
            f"このマークと一致する箇所を「探す場所」内で探します"
        )

    # ---------------------------------------------------------------- test
    def _set_test_result(
        self, msg: str, color: str, preview_bgr: np.ndarray | None,
    ) -> None:
        self._lbl_test_result.setText(msg)
        self._lbl_test_result.setStyleSheet(
            f"color:{color}; font-size:11px; font-weight:bold;"
        )
        if preview_bgr is None:
            self._lbl_test_preview.clear()
            self._lbl_test_preview.setText("")
        else:
            pix = _bgr_to_qpixmap(
                preview_bgr, _TEST_PREVIEW_MAX_W, _TEST_PREVIEW_MAX_H,
            )
            self._lbl_test_preview.setPixmap(pix)

    def _run_test(self) -> None:
        # window_title チェック
        if not self._window_title:
            self._set_test_result(
                "⚠ 対象ウィンドウが未設定（メイン画面で設定してください）",
                "#c62828", None,
            )
            return
        hwnd = find_hwnd_by_title(self._window_title)
        if not hwnd:
            self._set_test_result(
                f"⚠ ウィンドウが見つかりません: {self._window_title}",
                "#c62828", None,
            )
            return

        # テンプレ読込
        if not self._template_path or not os.path.isfile(self._template_path):
            self._set_test_result(
                f"⚠ テンプレ画像がありません: {self._template_path}",
                "#c62828", None,
            )
            return
        tmpl = cv2.imread(self._template_path, cv2.IMREAD_COLOR)
        if tmpl is None:
            self._set_test_result(
                "⚠ テンプレ画像を読み込めません", "#c62828", None,
            )
            return

        # キャプチャ
        img = capture_window_clean(hwnd)
        if img is None:
            self._set_test_result(
                "⚠ キャプチャ失敗", "#c62828", None,
            )
            return

        ih, iw = img.shape[:2]

        # 領域決定
        if self._chk_no_region.isChecked():
            x0 = y0 = 0
            target = img
        else:
            rx = self._spin_rx.value()
            ry = self._spin_ry.value()
            rw = self._spin_rw.value()
            rh = self._spin_rh.value()
            x0 = max(0, int(rx * iw))
            y0 = max(0, int(ry * ih))
            x1 = min(iw, int((rx + rw) * iw))
            y1 = min(ih, int((ry + rh) * ih))
            if x1 <= x0 or y1 <= y0:
                self._set_test_result(
                    "⚠ 探索領域のサイズが無効です（幅 or 高さが 0）",
                    "#c62828", None,
                )
                return
            target = img[y0:y1, x0:x1]

        # サイズチェック
        th_h, th_w = tmpl.shape[:2]
        if target.shape[0] < th_h or target.shape[1] < th_w:
            self._set_test_result(
                f"⚠ 探索領域がテンプレより小さい "
                f"(領域 {target.shape[1]}×{target.shape[0]} px / "
                f"テンプレ {th_w}×{th_h} px) → 検出不可。"
                "「幅」「高さ」を大きくしてください。",
                "#c62828", target.copy(),
            )
            return

        # matchTemplate
        res = cv2.matchTemplate(target, tmpl, cv2.TM_CCOEFF_NORMED)
        _, maxv, _, maxloc = cv2.minMaxLoc(res)
        threshold = self._spin_threshold.value()
        matched = bool(maxv >= threshold)

        # ハイライト枠を描画
        preview = target.copy()
        color_bgr = (0, 200, 0) if matched else (0, 0, 220)
        cv2.rectangle(
            preview, maxloc,
            (maxloc[0] + th_w, maxloc[1] + th_h),
            color_bgr, 2,
        )

        if matched:
            cx = x0 + maxloc[0] + th_w // 2
            cy = y0 + maxloc[1] + th_h // 2
            if self._spin_off_x is not None and self._spin_off_y is not None:
                cx += int(self._spin_off_x.value())
                cy += int(self._spin_off_y.value())
            msg = (
                f"✓ 一致  score={maxv:.3f}  threshold={threshold:.2f}\n"
                f"クリック位置≈ ({cx}, {cy})  "
                f"領域内位置=({maxloc[0]}, {maxloc[1]})"
            )
            self._set_test_result(msg, "#2e7d32", preview)
        else:
            msg = (
                f"✗ 不一致  best score={maxv:.3f}  threshold={threshold:.2f}\n"
                f"閾値を下げる / 探す場所を広げる / テンプレを再選択 を検討してください"
            )
            self._set_test_result(msg, "#c62828", preview)

    # ---------------------------------------------------------------- テンプレ変更
    def _change_template(self) -> None:
        """ゲーム画面をキャプチャ → ドラッグで新テンプレ領域選択 → 切出し保存。"""
        if not self._window_title:
            QMessageBox.information(
                self, "情報",
                "対象ウィンドウが未設定です（メイン画面で設定してください）",
            )
            return
        hwnd = find_hwnd_by_title(self._window_title)
        if not hwnd:
            QMessageBox.warning(
                self, "エラー",
                f"ウィンドウが見つかりません: {self._window_title}",
            )
            return

        # 現在の region を初期値として渡す
        init_rel = self._current_region_list()

        dlg = RegionPickerDialog(
            self._window_title, init_rel, self,
            dialog_title="新しいテンプレ画像の領域を選択",
            hint_text=(
                "ゲームウィンドウから新しいテンプレ画像を切り出します。"
                "ドラッグで探したい画像（マークやテキスト）を囲んでください。"
                "切り出した画像はそのまま templates/<シーン名>/ に保存されます。"
            ),
        )
        if not dlg.exec():
            return
        r = dlg.get_rel()
        if not r:
            return
        rx, ry, rw, rh = r

        # キャプチャして切り出し
        img = capture_window_clean(hwnd)
        if img is None:
            QMessageBox.warning(self, "エラー", "キャプチャ失敗")
            return
        ih, iw = img.shape[:2]
        x0 = max(0, int(rx * iw))
        y0 = max(0, int(ry * ih))
        x1 = min(iw, int((rx + rw) * iw))
        y1 = min(ih, int((ry + rh) * ih))
        if x1 <= x0 or y1 <= y0:
            QMessageBox.warning(self, "エラー", "選択した領域が小さすぎます")
            return
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            QMessageBox.warning(self, "エラー", "選択した領域が空です")
            return

        # 保存先: 既存テンプレと同じディレクトリ
        tpl_dir = (
            os.path.dirname(self._template_path).replace("\\", "/")
            if self._template_path
            else "templates"
        )
        os.makedirs(tpl_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        new_path = os.path.join(tpl_dir, f"tpl_{ts}.png").replace("\\", "/")
        if not cv2.imwrite(new_path, crop):
            QMessageBox.warning(self, "エラー", "保存に失敗しました")
            return

        # 差替え + プレビュー再ロード
        self._template_path = new_path
        self._lbl_tpl_path.setText(new_path)
        self._load_template_preview()

    def _pick_region(self) -> None:
        """ゲーム画面をキャプチャ → ドラッグで探す範囲を選択 → X/Y/W/H に反映。"""
        if not self._window_title:
            QMessageBox.information(
                self, "情報", "対象ウィンドウが未設定です",
            )
            return
        if not find_hwnd_by_title(self._window_title):
            QMessageBox.warning(
                self, "エラー",
                f"ウィンドウが見つかりません: {self._window_title}",
            )
            return
        init_rel = self._current_region_list()
        dlg = RegionPickerDialog(
            self._window_title, init_rel, self,
            dialog_title="探す場所を選択",
            hint_text=(
                "探したい画像が出現する範囲をドラッグで囲んでください。"
                "テンプレ画像より大きく（2〜4 倍が目安）取るとマッチしやすくなります。"
            ),
        )
        if not dlg.exec():
            return
        r = dlg.get_rel()
        if not r:
            return
        rx, ry, rw, rh = r
        # 「領域なし」を解除して X/Y/W/H へ反映
        self._chk_no_region.setChecked(False)
        self._spin_rx.setValue(rx)
        self._spin_ry.setValue(ry)
        self._spin_rw.setValue(rw)
        self._spin_rh.setValue(rh)

    def _current_region_list(self) -> list[float]:
        """現在のスピン値を [rx, ry, rw, rh] にして返す。「領域なし」なら空リスト。"""
        if self._chk_no_region.isChecked():
            return []
        return [
            self._spin_rx.value(),
            self._spin_ry.value(),
            self._spin_rw.value(),
            self._spin_rh.value(),
        ]

    # ---------------------------------------------------------------- 共通
    def _on_no_region_toggled(self, checked: bool) -> None:
        enabled = not checked
        for sp in (self._spin_rx, self._spin_ry, self._spin_rw, self._spin_rh):
            sp.setEnabled(enabled)

    def collect(self) -> dict:
        """OK 押下後に呼び出して、ステップ params に merge する更新値を返す。

        - `region` が `None` の場合は呼び出し側で params から削除すること
          （バックエンドは region なし = 全体探索として解釈する）
        - テンプレ画像をダイアログ内で差し替えた場合は `template` を含めて返す
        """
        out: dict = {
            "template": self._template_path,
            "threshold": float(self._spin_threshold.value()),
        }
        if self._spin_timeout is not None:
            out["timeout_s"] = float(self._spin_timeout.value())
        if self._chk_no_region.isChecked():
            out["region"] = None
        else:
            out["region"] = [
                round(self._spin_rx.value(), 4),
                round(self._spin_ry.value(), 4),
                round(self._spin_rw.value(), 4),
                round(self._spin_rh.value(), 4),
            ]
        if self._spin_off_x is not None and self._spin_off_y is not None:
            out["tap_offset_x"] = int(self._spin_off_x.value())
            out["tap_offset_y"] = int(self._spin_off_y.value())
        return out
