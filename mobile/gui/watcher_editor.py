"""ウォッチャー編集タブ。

watchers.json をフローとは独立して管理する。
新規作成/編集はスクショベースの1画面ダイアログ：
  左: スクショキャンバス（ズーム/パン/範囲選択）
  右: 検知方法 + 条件詳細 + アクション設定
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime as _dt

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QRadioButton, QSpinBox, QSplitter, QStackedWidget, QVBoxLayout,
    QWidget,
)

from .flow import (Condition, Watcher,
                   load_watcher, save_watcher, load_watchers_dir,
                   load_watchers)   # load_watchers は旧形式移行用
from .ocr_test_dialog import ImageCanvas
from .watcher_test_dialog import WatcherTestDialog

WATCHERS_DIR = "watchers"
TEMPLATES_DIR = "templates"
SCENES_DIR = "scenes"

_COND_LABELS = {
    "image_appear": "画像が出現したとき",
    "image_gone":   "画像が消えたとき",
    "ocr_number":   "数値で判定（OCR）",
}

_AFTER_LABELS = {
    "restart_scene": "現在のシーンを最初からやり直す",
    "next_scene":    "次のシーンへ進む",
    "noop":          "何もしない",
    "stop":          "フローを停止する",
}


# ------------------------------------------------------------------ ユーティリティ
def _np_to_pixmap(img: np.ndarray, max_w: int = 300, max_h: int = 120) -> QPixmap:
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    qimg = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888)
    pix = QPixmap.fromImage(qimg)
    return pix.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)


# ================================================================== ダイアログ
class _WatcherWizard(QDialog):
    """スクショベースのウォッチャー作成/編集ダイアログ（1画面）。"""

    def __init__(self, serial: str | None = None,
                 watcher: Watcher | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ウォッチャー設定")
        self.setMinimumSize(1000, 660)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowMinimizeButtonHint
        )

        self._serial = serial
        self._edit_watcher = watcher
        self._img:  np.ndarray | None = None
        self._crop: np.ndarray | None = None
        self._region: list[int] = []
        self._result: Watcher | None = None

        self._build_ui()
        if watcher:
            self._prefill(watcher)

    # =========================================================== UI 構築
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # タイトル行
        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("タイトル (必須):"))
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("例: ポーション低下、体力ピンチ、PVP攻撃")
        title_row.addWidget(self.title_edit, 1)
        root.addLayout(title_row)

        # スプリッター（左:キャンバス  右:設定）
        splitter = QSplitter(Qt.Horizontal)

        # ---- 左: キャンバス ----
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 4, 0)

        btn_row = QHBoxLayout()
        btn_cap = QPushButton("📷 スクショ取得")
        btn_cap.clicked.connect(self._capture)
        btn_file = QPushButton("📂 ファイル")
        btn_file.clicked.connect(self._open_file)
        btn_zoom = QPushButton("🔍 ズームリセット")
        btn_zoom.clicked.connect(lambda: self._canvas.reset_zoom())
        self._btn_recap = QPushButton("📷 再スクショ")
        self._btn_recap.setVisible(False)
        self._btn_recap.clicked.connect(self._retake_screenshot)
        btn_row.addWidget(btn_cap)
        btn_row.addWidget(btn_file)
        btn_row.addWidget(btn_zoom)
        btn_row.addWidget(self._btn_recap)
        btn_row.addStretch()
        left_lay.addLayout(btn_row)

        self._hint = QLabel("スクショを取得後、監視したい箇所をドラッグで囲んでください"
                            "  （ホイール:ズーム / 右ドラッグ:移動）")
        self._hint.setStyleSheet("color: #777; font-size: 10px;")
        left_lay.addWidget(self._hint)

        self._canvas = ImageCanvas()
        self._canvas.region_selected.connect(self._on_region_selected)
        left_lay.addWidget(self._canvas, 1)

        crop_row = QHBoxLayout()
        crop_row.addWidget(QLabel("選択範囲:"))
        self._crop_label = QLabel("（未選択）")
        self._crop_label.setFixedHeight(64)
        self._crop_label.setStyleSheet("border:1px solid #aaa; background:#111;")
        self._crop_label.setAlignment(Qt.AlignCenter)
        crop_row.addWidget(self._crop_label, 1)
        left_lay.addLayout(crop_row)

        splitter.addWidget(left)

        # ---- 右: 設定パネル ----
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(4, 0, 0, 0)

        # 検知方法
        grp_type = QGroupBox("検知方法")
        type_lay = QVBoxLayout(grp_type)
        self._type_group = QButtonGroup(self)
        self._rb_appear = QRadioButton("📷 画像が出現したとき（HP低下アイコン・PVP開始など）")
        self._rb_gone   = QRadioButton("📷 画像が消えたとき")
        self._rb_ocr    = QRadioButton("🔢 数値で判定（ポーション残量・HPなど）")
        self._rb_appear.setChecked(True)
        for i, rb in enumerate((self._rb_appear, self._rb_gone, self._rb_ocr)):
            self._type_group.addButton(rb, i)
            type_lay.addWidget(rb)
        self._type_group.idClicked.connect(self._on_type_changed)
        right_lay.addWidget(grp_type)

        # 条件詳細（スタック）
        grp_cond = QGroupBox("条件の詳細")
        cond_lay = QVBoxLayout(grp_cond)
        self._cond_stack = QStackedWidget()

        p_appear = QWidget()
        f_appear = QFormLayout(p_appear)
        self.threshold_appear = QDoubleSpinBox()
        self.threshold_appear.setRange(0.0, 1.0); self.threshold_appear.setSingleStep(0.01)
        self.threshold_appear.setValue(0.85); self.threshold_appear.setDecimals(2)
        f_appear.addRow("マッチ閾値 (0〜1):", self.threshold_appear)
        lbl = QLabel("選択範囲がこの画像と一致したときに発火します")
        lbl.setStyleSheet("color:#555; font-size:9px;"); lbl.setWordWrap(True)
        f_appear.addRow("", lbl)
        btn_match_appear = QPushButton("▶ マッチテスト（手動実行）")
        btn_match_appear.clicked.connect(self._run_match_test)
        f_appear.addRow("", btn_match_appear)
        self._match_result_appear = QLabel("← スクショを撮ってからテスト可")
        self._match_result_appear.setStyleSheet("color:#555; font-size:9px;")
        self._match_result_appear.setWordWrap(True)
        f_appear.addRow("マッチ結果:", self._match_result_appear)
        self._cond_stack.addWidget(p_appear)

        p_gone = QWidget()
        f_gone = QFormLayout(p_gone)
        self.threshold_gone = QDoubleSpinBox()
        self.threshold_gone.setRange(0.0, 1.0); self.threshold_gone.setSingleStep(0.01)
        self.threshold_gone.setValue(0.85); self.threshold_gone.setDecimals(2)
        f_gone.addRow("マッチ閾値 (0〜1):", self.threshold_gone)
        self.consecutive = QSpinBox()
        self.consecutive.setRange(1, 30); self.consecutive.setValue(3)
        f_gone.addRow("連続ミス回数:", self.consecutive)
        lbl2 = QLabel("選択範囲の画像がN回連続して検出されなくなったときに発火します")
        lbl2.setStyleSheet("color:#555; font-size:9px;"); lbl2.setWordWrap(True)
        f_gone.addRow("", lbl2)
        btn_match_gone = QPushButton("▶ マッチテスト（手動実行）")
        btn_match_gone.clicked.connect(self._run_match_test)
        f_gone.addRow("", btn_match_gone)
        self._match_result_gone = QLabel("← スクショを撮ってからテスト可")
        self._match_result_gone.setStyleSheet("color:#555; font-size:9px;")
        self._match_result_gone.setWordWrap(True)
        f_gone.addRow("マッチ結果:", self._match_result_gone)
        self._cond_stack.addWidget(p_gone)

        p_ocr = QWidget()
        f_ocr = QFormLayout(p_ocr)
        self.ocr_whitelist = QLineEdit("0123456789")
        self.ocr_whitelist.textChanged.connect(lambda _: self._run_ocr_test())
        f_ocr.addRow("読み取る文字種:", self.ocr_whitelist)
        op_row = QHBoxLayout()
        self.ocr_op = QComboBox()
        for op in ("<", "<=", ">", ">=", "=="):
            self.ocr_op.addItem(op, op)
        self.ocr_op.setCurrentIndex(1)
        op_row.addWidget(self.ocr_op)
        self.ocr_value = QSpinBox(); self.ocr_value.setRange(0, 99999)
        op_row.addWidget(self.ocr_value); op_row.addStretch()
        f_ocr.addRow("発火条件 (数値):", op_row)
        self.ocr_consecutive = QSpinBox()
        self.ocr_consecutive.setRange(1, 30)
        self.ocr_consecutive.setValue(1)
        self.ocr_consecutive.setSuffix(" 回連続")
        lbl_cons = QLabel("1=即時発火、2以上=N回連続で条件を満たしたとき発火（誤検知対策）")
        lbl_cons.setStyleSheet("color:#555; font-size:9px;")
        lbl_cons.setWordWrap(True)
        f_ocr.addRow("連続検知回数:", self.ocr_consecutive)
        f_ocr.addRow("", lbl_cons)
        btn_ocr_test = QPushButton("▶ OCRテスト（手動実行）")
        btn_ocr_test.clicked.connect(self._run_ocr_test)
        f_ocr.addRow("", btn_ocr_test)
        self._ocr_result_lbl = QLabel("← 範囲を選択すると自動実行")
        self._ocr_result_lbl.setStyleSheet("color:#555; font-size:9px;")
        self._ocr_result_lbl.setWordWrap(True)
        f_ocr.addRow("OCR結果:", self._ocr_result_lbl)
        self._cond_stack.addWidget(p_ocr)

        cond_lay.addWidget(self._cond_stack)
        right_lay.addWidget(grp_cond)

        # アクション
        grp_act = QGroupBox("発火時のアクション")
        act_lay = QFormLayout(grp_act)
        hh = QHBoxLayout()
        self.handler_edit = QLineEdit()
        self.handler_edit.setPlaceholderText("scenes/ 以下の .json（省略可）")
        btn_h = QPushButton("参照"); btn_h.setFixedWidth(50)
        btn_h.clicked.connect(self._browse_handler)
        hh.addWidget(self.handler_edit, 1); hh.addWidget(btn_h)
        act_lay.addRow("実行シーン:", hh)
        self.after_combo = QComboBox()
        for key, label in _AFTER_LABELS.items():
            self.after_combo.addItem(label, key)
        # 新規ウォッチャーのデフォルトは「何もしない」（編集時は _prefill で上書き）
        _idx_noop = self.after_combo.findData("noop")
        if _idx_noop >= 0:
            self.after_combo.setCurrentIndex(_idx_noop)
        act_lay.addRow("完了後:", self.after_combo)
        self.cooldown_spin = QDoubleSpinBox()
        self.cooldown_spin.setRange(0, 3600); self.cooldown_spin.setSingleStep(1.0)
        self.cooldown_spin.setSuffix(" 秒")
        act_lay.addRow("クールダウン:", self.cooldown_spin)
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(0, 999)
        act_lay.addRow("優先度 (大きいほど優先):", self.priority_spin)
        poll_row = QHBoxLayout()
        self.poll_min_spin = QDoubleSpinBox()
        self.poll_min_spin.setRange(0.0, 3600.0); self.poll_min_spin.setSingleStep(0.5)
        self.poll_min_spin.setDecimals(1); self.poll_min_spin.setSuffix(" 秒")
        self.poll_min_spin.setSpecialValueText("全体設定")
        self.poll_max_spin = QDoubleSpinBox()
        self.poll_max_spin.setRange(0.0, 3600.0); self.poll_max_spin.setSingleStep(0.5)
        self.poll_max_spin.setDecimals(1); self.poll_max_spin.setSuffix(" 秒")
        self.poll_max_spin.setSpecialValueText("固定")
        poll_row.addWidget(QLabel("最小:"))
        poll_row.addWidget(self.poll_min_spin)
        poll_row.addWidget(QLabel("〜  最大:"))
        poll_row.addWidget(self.poll_max_spin)
        poll_row.addStretch()
        act_lay.addRow("ポーリング間隔:", poll_row)
        lbl_poll = QLabel("0=全体設定を使用。最大>最小のときランダム間隔")
        lbl_poll.setStyleSheet("color:#555; font-size:9px;")
        act_lay.addRow("", lbl_poll)
        self.enabled_check = QCheckBox("有効")
        self.enabled_check.setChecked(True)
        act_lay.addRow("", self.enabled_check)
        self.alert_check = QCheckBox(
            "🔔 発火時に通知（デスクトップ + Google Chat — 設定がある場合のみ）"
        )
        self.alert_check.setToolTip(
            "オンにすると、Windows トースト通知と Google Chat への Webhook 通知を送ります。\n"
            "Google Chat の Webhook URL は設定ダイアログから入力してください。"
        )
        act_lay.addRow("", self.alert_check)
        right_lay.addWidget(grp_act)
        right_lay.addStretch()

        splitter.addWidget(right)
        splitter.setSizes([620, 380])
        root.addWidget(splitter, 1)

        # ボタン行
        nav = QHBoxLayout()
        btn_cancel = QPushButton("キャンセル")
        btn_cancel.clicked.connect(self.reject)
        self._btn_ok = QPushButton("✓ 確定")
        self._btn_ok.setStyleSheet(
            "QPushButton{background:#1565c0;color:white;font-weight:bold;padding:6px;}"
            "QPushButton:hover{background:#0d47a1;}"
        )
        self._btn_ok.clicked.connect(self._on_ok)
        nav.addWidget(btn_cancel)
        nav.addStretch()
        nav.addWidget(self._btn_ok)
        root.addLayout(nav)

    # =========================================================== キャンバス操作
    def _capture(self) -> None:
        if not self._serial:
            QMessageBox.information(self, "情報",
                "デバイスが接続されていません。\n"
                "メイン画面でデバイスに接続してから実行してください。")
            return
        self._load_screenshot(keep_region=True)

    def _retake_screenshot(self) -> None:
        """OCR確認用: 既存の選択領域を保持したまま再スクショ。"""
        if not self._serial:
            QMessageBox.information(self, "情報", "デバイスが接続されていません。")
            return
        self._load_screenshot(keep_region=True)

    def _load_screenshot(self, keep_region: bool = False) -> None:
        try:
            from .adb import screencap
            self._hint.setText("取得中…")
            self.repaint()
            png = screencap(self._serial)
            arr = np.frombuffer(png, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError(f"デコード失敗 (header={png[:8].hex()})")
            self._img = img
            self._canvas.set_image(img)
            self._hint.setText("監視したい箇所をドラッグで囲んでください")
            if keep_region and self._region:
                region = list(self._region)
                QTimer.singleShot(50, lambda: (
                    self._canvas.highlight_region(*region),
                    self._run_ocr_test(),
                    self._run_match_test(),
                ))
        except Exception as e:
            import traceback
            print(f"[screencap error]\n{traceback.format_exc()}")
            QMessageBox.critical(self, "スクショ取得失敗",
                                 f"{e}\n\n詳細はターミナルに出力されています")

    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "画像を開く", "", "画像 (*.png *.jpg *.bmp)")
        if not path:
            return
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            QMessageBox.critical(self, "エラー", f"画像を開けませんでした: {path}")
            return
        self._img = img
        self._canvas.set_image(img)
        self._hint.setText("監視したい箇所をドラッグで囲んでください")
        if self._region:
            region = list(self._region)
            QTimer.singleShot(50, lambda: self._canvas.highlight_region(*region))

    def _on_region_selected(self, x: int, y: int, w: int, h: int) -> None:
        self._region = [x, y, w, h]
        if self._img is None:
            return
        ih, iw = self._img.shape[:2]
        crop = self._img[max(0, y):min(y + h, ih), max(0, x):min(x + w, iw)]
        if crop.size == 0:
            return
        self._crop = crop.copy()
        pix = _np_to_pixmap(crop, 600, 64)
        self._crop_label.setPixmap(pix)
        self._crop_label.setText("")
        # 種別に応じてテスト自動実行
        ctype_idx = self._type_group.checkedId()
        if ctype_idx == 2:
            self._run_ocr_test()
        elif ctype_idx in (0, 1):
            self._run_match_test()

    def _on_type_changed(self, idx: int) -> None:
        self._cond_stack.setCurrentIndex(idx)
        is_ocr = (idx == 2)
        self._btn_recap.setVisible(is_ocr)
        if is_ocr and self._region:
            self._run_ocr_test()

    # =========================================================== OCR
    def _run_ocr_test(self) -> None:
        if self._img is None or not self._region:
            self._ocr_result_lbl.setText("← 範囲を選択すると自動実行")
            self._ocr_result_lbl.setStyleSheet("color:#555; font-size:9px;")
            return
        try:
            import pytesseract
        except ImportError:
            QMessageBox.warning(self, "未インストール",
                "pytesseract がインストールされていません。\n"
                "pip install pytesseract を実行してください。")
            return
        x, y, w, h = self._region
        ih, iw = self._img.shape[:2]
        crop = self._img[max(0, y):min(y + h, ih), max(0, x):min(x + w, iw)]
        if crop.size == 0:
            return
        wl = self.ocr_whitelist.text().strip()
        config = "--psm 7 --oem 3"
        if wl:
            config += f" -c tessedit_char_whitelist={wl}"
        try:
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            _, gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            text = pytesseract.image_to_string(gray, config=config).strip()
            if text:
                self._ocr_result_lbl.setText(text)
                self._ocr_result_lbl.setStyleSheet(
                    "font-weight:bold; color:#1b5e20; font-size:16px;")
            else:
                self._ocr_result_lbl.setText(
                    "読み取れませんでした — 範囲や文字種を変更してみてください")
                self._ocr_result_lbl.setStyleSheet("font-weight:bold; color:#c62828;")
        except Exception as e:
            self._ocr_result_lbl.setText(f"エラー: {e}")

    # =========================================================== 画像マッチテスト
    def _run_match_test(self) -> None:
        ctype_idx = self._type_group.checkedId()
        if ctype_idx not in (0, 1):
            return
        lbl = self._match_result_appear if ctype_idx == 0 else self._match_result_gone
        threshold = self.threshold_appear.value() if ctype_idx == 0 else self.threshold_gone.value()

        if self._img is None or self._crop is None or not self._region:
            lbl.setText("← スクショを撮ってからテスト可")
            lbl.setStyleSheet("color:#555; font-size:9px;")
            return
        x, y, w, h = self._region
        ih, iw = self._img.shape[:2]
        target = self._img[max(0, y):min(y + h, ih), max(0, x):min(x + w, iw)]
        tmpl = self._crop
        if target.size == 0 or target.shape[0] < tmpl.shape[0] or target.shape[1] < tmpl.shape[1]:
            lbl.setText("エラー: region がテンプレより小さいか範囲外")
            lbl.setStyleSheet("color:#c62828; font-size:9px;")
            return
        res = cv2.matchTemplate(target, tmpl, cv2.TM_CCOEFF_NORMED)
        _, maxv, _, _ = cv2.minMaxLoc(res)
        hit = maxv >= threshold
        margin = maxv - threshold
        sign = "+" if margin >= 0 else ""
        status = "✅ 発火" if hit else "❌ 不発火"
        lbl.setText(f"{status}  スコア: {maxv:.3f}  マージン: {sign}{margin:.3f}")
        lbl.setStyleSheet(
            "font-weight:bold; color:#1b5e20; font-size:13px;"
            if hit else
            "font-weight:bold; color:#c62828; font-size:13px;"
        )

    # =========================================================== アクション
    def _browse_handler(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "ハンドラシーン選択", SCENES_DIR, "JSON (*.json)")
        if path:
            self.handler_edit.setText(
                os.path.relpath(path, SCENES_DIR).replace("\\", "/"))

    # =========================================================== 確定・読込
    def _on_ok(self) -> None:
        if not self.title_edit.text().strip():
            QMessageBox.warning(self, "入力エラー", "タイトルを入力してください")
            return

        ctype_idx = self._type_group.checkedId()
        ctype = ["image_appear", "image_gone", "ocr_number"][ctype_idx]

        template_path = ""
        if ctype in ("image_appear", "image_gone"):
            if self._crop is None:
                QMessageBox.warning(self, "エラー", "スクショ範囲が選択されていません")
                return
            os.makedirs(TEMPLATES_DIR, exist_ok=True)
            wid = (self._edit_watcher.id if self._edit_watcher else str(uuid.uuid4())[:8])
            fname = f"{wid}_{ctype}.png"
            template_path = os.path.join(TEMPLATES_DIR, fname).replace("\\", "/")
            cv2.imwrite(template_path, self._crop)
        elif ctype == "ocr_number" and not self._region:
            QMessageBox.warning(self, "エラー", "OCR の読み取り範囲を選択してください")
            return

        region = list(self._region)
        if ctype == "image_appear":
            cond = Condition(type="image_appear", template=template_path,
                             region=region, threshold=self.threshold_appear.value())
        elif ctype == "image_gone":
            cond = Condition(type="image_gone", template=template_path,
                             region=region, threshold=self.threshold_gone.value(),
                             consecutive=self.consecutive.value())
        else:
            cond = Condition(type="ocr_number", region=region,
                             ocr_whitelist=self.ocr_whitelist.text().strip(),
                             op=self.ocr_op.currentData(),
                             value=self.ocr_value.value(),
                             consecutive=self.ocr_consecutive.value())

        wid = (self._edit_watcher.id if self._edit_watcher else str(uuid.uuid4())[:8])
        poll_min = self.poll_min_spin.value()
        poll_max = self.poll_max_spin.value() if self.poll_max_spin.value() > poll_min else 0.0
        self._result = Watcher(
            id=wid,
            title=self.title_edit.text().strip(),
            enabled=self.enabled_check.isChecked(),
            priority=self.priority_spin.value(),
            condition=cond,
            handler=self.handler_edit.text().strip(),
            after=self.after_combo.currentData(),
            cooldown_s=self.cooldown_spin.value(),
            interrupt="step_end",
            alert_desktop=self.alert_check.isChecked(),
            poll_min_s=poll_min,
            poll_max_s=poll_max,
        )
        self.accept()

    def _prefill(self, w: Watcher) -> None:
        self.title_edit.setText(w.title)
        self.enabled_check.setChecked(w.enabled)
        self.alert_check.setChecked(w.alert_desktop)
        self.priority_spin.setValue(w.priority)
        self.handler_edit.setText(w.handler)
        idx = self.after_combo.findData(w.after)
        if idx >= 0:
            self.after_combo.setCurrentIndex(idx)
        self.cooldown_spin.setValue(w.cooldown_s)
        self.poll_min_spin.setValue(w.poll_min_s)
        self.poll_max_spin.setValue(w.poll_max_s)

        ctype = w.condition.type
        if ctype == "image_appear":
            self._rb_appear.setChecked(True); self._cond_stack.setCurrentIndex(0)
            self.threshold_appear.setValue(w.condition.threshold)
        elif ctype == "image_gone":
            self._rb_gone.setChecked(True); self._cond_stack.setCurrentIndex(1)
            self.threshold_gone.setValue(w.condition.threshold)
            self.consecutive.setValue(w.condition.consecutive)
        elif ctype == "ocr_number":
            self._rb_ocr.setChecked(True); self._cond_stack.setCurrentIndex(2)
            self._btn_recap.setVisible(True)
            self.ocr_whitelist.setText(w.condition.ocr_whitelist)
            idx2 = self.ocr_op.findData(w.condition.op)
            if idx2 >= 0:
                self.ocr_op.setCurrentIndex(idx2)
            self.ocr_value.setValue(w.condition.value)
            self.ocr_consecutive.setValue(max(1, w.condition.consecutive))

        self._region = list(w.condition.region) if w.condition.region else []

        if ctype in ("image_appear", "image_gone") and w.condition.template:
            img = cv2.imread(w.condition.template, cv2.IMREAD_COLOR)
            if img is not None:
                self._crop = img
                self._img  = img
                self._canvas.set_image(img)
                # テンプレート画像 = 切り抜いた領域そのものなので全体をハイライト
                h_img, w_img = img.shape[:2]
                QTimer.singleShot(50, lambda wi=w_img, hi=h_img:
                    self._canvas.highlight_region(0, 0, wi, hi))
                pix = _np_to_pixmap(img, 600, 64)
                self._crop_label.setPixmap(pix)
                self._crop_label.setText("")

    def result_watcher(self) -> Watcher | None:
        return self._result


# ================================================================== 経験値計測
_EXP_METER_SETTINGS    = "exp_meter.json"
_EXP_SAMPLE_INTERVAL_MS = 3 * 60 * 1000  # 3分
_EXP_CURRENT_WINDOW    = 3               # 現在速度: 直近Nサンプルで算出
_EXP_OCR_TRIES         = 3               # 1回の計測で取るOCR試行回数
_EXP_OCR_INTERVAL_S    = 1.5            # OCR試行間隔（秒）


class _RegionPickerDialog(QDialog):
    """経験値%の表示領域をスクショから選択するダイアログ。"""

    def __init__(self, serial: str | None, region: list[int],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("経験値%の表示領域を設定")
        self.setMinimumSize(800, 540)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)
        self._serial = serial
        self._region = list(region)
        self._img: np.ndarray | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        hint = QLabel("スクショを取得して、経験値%が表示されている領域をドラッグで囲んでください")
        hint.setStyleSheet("color: #555; font-size: 10px;")
        lay.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_cap  = QPushButton("📷 スクショ取得")
        btn_file = QPushButton("📂 ファイル")
        btn_zoom = QPushButton("🔍 ズームリセット")
        btn_cap.clicked.connect(self._capture)
        btn_file.clicked.connect(self._open_file)
        btn_zoom.clicked.connect(lambda: self._canvas.reset_zoom())
        for b in (btn_cap, btn_file, btn_zoom):
            btn_row.addWidget(b)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        self._canvas = ImageCanvas()
        self._canvas.region_selected.connect(self._on_region)
        lay.addWidget(self._canvas, 1)

        self._region_lbl = QLabel(
            "選択範囲: （未選択）" if not self._region else
            f"選択範囲: x={self._region[0]} y={self._region[1]} "
            f"w={self._region[2]} h={self._region[3]}"
        )
        lay.addWidget(self._region_lbl)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _capture(self) -> None:
        if not self._serial:
            QMessageBox.information(self, "情報", "デバイスが接続されていません")
            return
        try:
            from .adb import screencap
            png = screencap(self._serial)
            arr = np.frombuffer(png, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("デコード失敗")
            self._img = img
            self._canvas.set_image(img)
            if self._region:
                QTimer.singleShot(50, lambda: self._canvas.highlight_region(*self._region))
        except Exception as e:
            QMessageBox.critical(self, "エラー", str(e))

    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "画像を開く", "", "画像 (*.png *.jpg *.bmp)")
        if not path:
            return
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            QMessageBox.critical(self, "エラー", f"開けません: {path}")
            return
        self._img = img
        self._canvas.set_image(img)

    def _on_region(self, x: int, y: int, w: int, h: int) -> None:
        self._region = [x, y, w, h]
        self._region_lbl.setText(f"選択範囲: x={x} y={y} w={w} h={h}")

    def get_region(self) -> list[int]:
        return self._region


class ExpMeterWidget(QWidget):
    """経験値計測パネル。5分おきにOCRして成長速度を計測・表示する。

    - 現在速度: 直近3サンプル（約15分）の変化率
    - 平均速度: リセット後の全サンプルから算出
    - ロールオーバー: 値が30%超の急落でレベルアップと判定し累積に加算
    """

    _sample_ready  = Signal(float)
    _sample_failed = Signal(str)

    def __init__(self, main_window) -> None:
        super().__init__()
        self._mw          = main_window
        self._region:      list[int]              = []
        self._samples:     list[tuple[_dt, float]] = []  # (timestamp, accumulated_pct)
        self._prev_raw:    float | None            = None
        self._accumulated: float                   = 0.0
        self._start_time:  _dt | None              = None
        self._running      = False

        self._sample_ready.connect(self._on_sample_ready)
        self._sample_failed.connect(self._on_sample_failed)

        self._timer = QTimer(self)
        self._timer.setInterval(_EXP_SAMPLE_INTERVAL_MS)
        self._timer.timeout.connect(self._do_sample)

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(60_000)
        self._clock_timer.timeout.connect(self._update_display)
        self._clock_timer.start()

        self._build_ui()
        self._load_settings()

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        grp = QGroupBox("📊 経験値計測")
        grp_lay = QVBoxLayout(grp)
        grp_lay.setSpacing(4)

        btn_row = QHBoxLayout()
        self._btn_start = QPushButton("▶ 計測開始")
        self._btn_start.setFixedWidth(110)
        self._btn_start.clicked.connect(self._toggle_running)
        btn_region = QPushButton("📍 領域設定")
        btn_region.setFixedWidth(100)
        btn_region.clicked.connect(self._pick_region)
        self._btn_reset = QPushButton("🔄 リセット")
        self._btn_reset.setFixedWidth(90)
        self._btn_reset.clicked.connect(self._reset)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: #666; font-size: 10px;")
        btn_row.addWidget(self._btn_start)
        btn_row.addWidget(btn_region)
        btn_row.addWidget(self._btn_reset)
        btn_row.addWidget(self._status_lbl)
        btn_row.addStretch()
        grp_lay.addLayout(btn_row)

        hint_row = QHBoxLayout()
        hint_lbl = QLabel("桁数ヒント:")
        hint_lbl.setStyleSheet("font-size: 11px;")
        self._rb_1digit = QRadioButton("1桁  (0〜9%台)")
        self._rb_2digit = QRadioButton("2桁  (10〜99%台)")
        self._rb_1digit.setChecked(True)
        self._rb_1digit.toggled.connect(self._save_settings)
        for rb in (self._rb_1digit, self._rb_2digit):
            rb.setStyleSheet("font-size: 11px;")
        hint_row.addWidget(hint_lbl)
        hint_row.addWidget(self._rb_1digit)
        hint_row.addWidget(self._rb_2digit)
        hint_row.addStretch()
        grp_lay.addLayout(hint_row)

        stats_row1 = QHBoxLayout()
        self._lbl_current       = QLabel("現在値:  —")
        self._lbl_current_speed = QLabel("現在速度:  —")
        self._lbl_compare       = QLabel("")
        for lbl in (self._lbl_current, self._lbl_current_speed, self._lbl_compare):
            lbl.setStyleSheet("font-size: 12px;")
        stats_row1.addWidget(self._lbl_current)
        stats_row1.addSpacing(24)
        stats_row1.addWidget(self._lbl_current_speed)
        stats_row1.addSpacing(8)
        stats_row1.addWidget(self._lbl_compare)
        stats_row1.addStretch()
        grp_lay.addLayout(stats_row1)

        stats_row2 = QHBoxLayout()
        self._lbl_avg_speed = QLabel("平均速度:  —")
        self._lbl_avg_speed.setStyleSheet("font-size: 12px;")
        self._lbl_meta = QLabel("")
        self._lbl_meta.setStyleSheet("color: #777; font-size: 10px;")
        stats_row2.addWidget(self._lbl_avg_speed)
        stats_row2.addSpacing(24)
        stats_row2.addWidget(self._lbl_meta)
        stats_row2.addStretch()
        grp_lay.addLayout(stats_row2)

        stats_row3 = QHBoxLayout()
        self._lbl_eta = QLabel("LvUP予測:  —")
        self._lbl_eta.setStyleSheet("font-size: 12px;")
        stats_row3.addWidget(self._lbl_eta)
        stats_row3.addStretch()
        grp_lay.addLayout(stats_row3)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(grp)
        self._update_display()

    # ---------------------------------------------------------------- 設定・データ永続化
    def _load_settings(self) -> None:
        import json
        if not os.path.exists(_EXP_METER_SETTINGS):
            return
        try:
            with open(_EXP_METER_SETTINGS, "r", encoding="utf-8") as f:
                d = json.load(f)
            self._region = d.get("region", [])
            if d.get("digit_hint", 1) == 2:
                self._rb_2digit.setChecked(True)
            # サンプルデータの復元
            raw_samples = d.get("samples", [])
            self._samples = [
                (_dt.fromisoformat(ts), float(acc))
                for ts, acc in raw_samples
            ]
            self._accumulated = float(d.get("accumulated", 0.0))
            self._prev_raw    = (float(d["prev_raw"]) if d.get("prev_raw") is not None
                                 else None)
            st = d.get("start_time")
            self._start_time  = _dt.fromisoformat(st) if st else None
            if self._samples:
                self._update_display()
        except Exception:
            pass

    def _save_settings(self) -> None:
        import json
        try:
            with open(_EXP_METER_SETTINGS, "w", encoding="utf-8") as f:
                json.dump({
                    "region":     self._region,
                    "digit_hint": 2 if self._rb_2digit.isChecked() else 1,
                    "samples":    [[ts.isoformat(), acc]
                                   for ts, acc in self._samples],
                    "accumulated": self._accumulated,
                    "prev_raw":    self._prev_raw,
                    "start_time":  (self._start_time.isoformat()
                                    if self._start_time else None),
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _pick_region(self) -> None:
        dlg = _RegionPickerDialog(
            serial=self._mw.current_serial,
            region=self._region,
            parent=self,
        )
        if dlg.exec() == QDialog.Accepted:
            r = dlg.get_region()
            if r:
                self._region = r
                self._save_settings()

    # ---------------------------------------------------------------- 計測制御
    def _toggle_running(self) -> None:
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        if not self._region:
            QMessageBox.information(
                self, "情報",
                "先に「📍 領域設定」で経験値%が表示される領域を選択してください。")
            return
        if not self._mw.current_serial:
            QMessageBox.information(self, "情報", "デバイスが接続されていません。")
            return
        self._running = True
        if self._start_time is None:
            self._start_time = _dt.now()
        self._btn_start.setText("■ 停止")
        self._btn_start.setStyleSheet(
            "QPushButton{background:#c62828;color:white;font-weight:bold;}")
        self._timer.start()
        self._status_lbl.setText("計測中…")
        self._do_sample()

    def _stop(self) -> None:
        self._running = False
        self._timer.stop()
        self._btn_start.setText("▶ 計測開始")
        self._btn_start.setStyleSheet("")
        self._status_lbl.setText("停止中")

    def _reset(self) -> None:
        was_running = self._running
        self._stop()
        self._samples.clear()
        self._prev_raw    = None
        self._accumulated = 0.0
        self._start_time  = None
        self._update_display()
        self._save_settings()
        self._status_lbl.setText("")
        if was_running:
            self._start()

    # ---------------------------------------------------------------- サンプリング
    def _log(self, msg: str) -> None:
        """logs/YYYY-MM-DD.log に経験値計測のログ行を追記する。"""
        now = _dt.now()
        line = f"[{now.strftime('%H:%M:%S')}] 📊 経験値計測: {msg}\n"
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        try:
            with open(
                os.path.join(log_dir, f"{now.strftime('%Y-%m-%d')}.log"),
                "a", encoding="utf-8",
            ) as f:
                f.write(line)
        except Exception:
            pass

    def _do_sample(self) -> None:
        serial = self._mw.current_serial
        if not serial or not self._region:
            return
        self._status_lbl.setText("取得中…")
        digit_hint = 2 if self._rb_2digit.isChecked() else 1
        import threading
        threading.Thread(
            target=self._sample_worker,
            args=(serial, list(self._region), digit_hint),
            daemon=True,
        ).start()

    @staticmethod
    def _apply_digit_hint(ocr_raw: str, hint: int) -> float | None:
        """OCR文字列に桁数ヒントを適用して float を返す。

        数字のみ抽出 → hint 桁目の後ろに小数点を挿入 → float変換。
        数字が hint 桁に満たない場合は None。
        """
        pure = "".join(c for c in ocr_raw if c.isdigit())
        if len(pure) <= hint:
            return None
        fixed = pure[:hint] + "." + pure[hint:]
        val = float(fixed)
        return val if 0.0 <= val <= 100.0 else None

    def _sample_worker(self, serial: str, region: list[int], digit_hint: int) -> None:
        """_EXP_OCR_TRIES 回スクショ+OCRを行い、中央値を採用して emit する。"""
        import time
        try:
            from .adb import screencap
            from .flow_runner import _ocr_digits_best
            config = "--psm 7 --oem 3 -c tessedit_char_whitelist=0123456789."
            x, y, w, h = region
            readings: list[float] = []
            raw_log:  list[str]   = []

            for i in range(_EXP_OCR_TRIES):
                if i > 0:
                    time.sleep(_EXP_OCR_INTERVAL_S)
                try:
                    png = screencap(serial)
                    arr = np.frombuffer(png, dtype=np.uint8)
                    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if img is None:
                        raw_log.append(f"#{i+1}:デコード失敗")
                        continue
                    ih, iw = img.shape[:2]
                    crop = img[max(0, y):min(y + h, ih), max(0, x):min(x + w, iw)]
                    if crop.size == 0:
                        raw_log.append(f"#{i+1}:領域外")
                        continue
                    digits, _ = _ocr_digits_best(crop, config)
                    if not digits:
                        raw_log.append(f"#{i+1}:OCR失敗")
                        continue
                    val = self._apply_digit_hint(digits, digit_hint)
                    if val is None:
                        raw_log.append(f"#{i+1}:{digits!r}→ヒント適用失敗")
                        continue
                    readings.append(val)
                    raw_log.append(f"#{i+1}:{digits!r}→{val:.4f}%")
                except Exception as e:
                    raw_log.append(f"#{i+1}:エラー({e})")

            summary = "  ".join(raw_log)
            if not readings:
                self._sample_failed.emit(f"全試行失敗 [{summary}]")
                return

            readings.sort()
            chosen = readings[len(readings) // 2]  # 中央値
            self._last_ocr_detail = f"採用={chosen:.4f}%  hint={digit_hint}桁  試行=[{summary}]"
            self._sample_ready.emit(chosen)
        except Exception as e:
            self._sample_failed.emit(str(e))

    def _on_sample_ready(self, raw: float) -> None:
        now = _dt.now()
        detail = getattr(self, "_last_ocr_detail", "")
        if self._prev_raw is None:
            # 初回サンプル: 累積0からスタート
            self._prev_raw    = raw
            self._accumulated = 0.0
            self._samples.append((now, 0.0))
            self._log(f"初回取得  累積=0.0000%  {detail}")
        else:
            delta = raw - self._prev_raw
            if delta < -30:
                # レベルアップ: (100 - prev) + raw の分だけ増加
                valid_delta = (100.0 - self._prev_raw) + raw
                self._accumulated += valid_delta
                self._prev_raw = raw
                self._samples.append((now, self._accumulated))
                self._log(
                    f"LvUP検知  delta={delta:+.4f}%"
                    f"  加算={valid_delta:.4f}%  累積={self._accumulated:.4f}%  {detail}"
                )
            elif delta < 0:
                # OCR誤読とみなしてスキップ（prev_rawは更新しない）
                self._log(
                    f"誤読スキップ  delta={delta:+.4f}%"
                    f"  prev={self._prev_raw:.4f}%  {detail}"
                )
                self._status_lbl.setText(
                    f"⚠ 誤読スキップ ({raw:.2f}%)  最終: {now.strftime('%H:%M')}")
                return
            else:
                self._accumulated += delta
                self._prev_raw = raw
                self._samples.append((now, self._accumulated))
                self._log(
                    f"取得  delta={delta:+.4f}%"
                    f"  累積={self._accumulated:.4f}%  {detail}"
                )

        self._status_lbl.setText(f"最終取得: {now.strftime('%H:%M')}")
        self._update_display()
        self._save_settings()

    def _on_sample_failed(self, msg: str) -> None:
        self._log(f"エラー  {msg}")
        self._status_lbl.setText(f"⚠ {msg}")

    # ---------------------------------------------------------------- 速度計算・表示
    @staticmethod
    def _calc_speed(samples: list[tuple[_dt, float]]) -> float | None:
        """サンプルリスト（2点以上）から %/h を返す。"""
        if len(samples) < 2:
            return None
        elapsed_h = (samples[-1][0] - samples[0][0]).total_seconds() / 3600
        if elapsed_h <= 0:
            return None
        return (samples[-1][1] - samples[0][1]) / elapsed_h

    def _update_display(self) -> None:
        n = len(self._samples)

        self._lbl_current.setText(
            f"現在値:  {self._prev_raw:.2f}%"
            if self._prev_raw is not None else "現在値:  —"
        )

        avg_spd = self._calc_speed(self._samples)
        cur_spd = (self._calc_speed(self._samples[-_EXP_CURRENT_WINDOW:])
                   if n >= _EXP_CURRENT_WINDOW else None)

        self._lbl_avg_speed.setText(
            f"平均速度:  {avg_spd:.1f} %/h" if avg_spd is not None else "平均速度:  —"
        )
        if cur_spd is not None:
            self._lbl_current_speed.setText(f"現在速度:  {cur_spd:.1f} %/h")
        elif n > 0:
            self._lbl_current_speed.setText(
                f"現在速度:  — (あと{_EXP_CURRENT_WINDOW - n}サンプル)")
        else:
            self._lbl_current_speed.setText("現在速度:  —")

        if cur_spd is not None and avg_spd is not None:
            diff = cur_spd - avg_spd
            if diff >= 0.1:
                self._lbl_compare.setText(f"↑ 平均より +{diff:.1f}%/h")
                self._lbl_compare.setStyleSheet(
                    "color:#1b5e20; font-weight:bold; font-size:12px;")
            elif diff <= -0.1:
                self._lbl_compare.setText(f"↓ 平均より {diff:.1f}%/h")
                self._lbl_compare.setStyleSheet(
                    "color:#c62828; font-weight:bold; font-size:12px;")
            else:
                self._lbl_compare.setText("≈ 平均と同程度")
                self._lbl_compare.setStyleSheet("color:#555; font-size:12px;")
        else:
            self._lbl_compare.setText("")

        elapsed_str = "—"
        if self._start_time:
            s = int((_dt.now() - self._start_time).total_seconds())
            h, rem = divmod(s, 3600)
            elapsed_str = f"{h}h {rem // 60:02d}m" if h > 0 else f"{rem // 60}m"
        last_str = self._samples[-1][0].strftime("%H:%M") if self._samples else "—"
        self._lbl_meta.setText(f"計測: {elapsed_str}  {n}サンプル  最終: {last_str}")

        # LvUP予測
        if self._prev_raw is not None:
            remaining = 100.0 - self._prev_raw
            parts = []
            for spd, label in ((cur_spd, "現在"), (avg_spd, "平均")):
                if spd and spd > 0:
                    eta_min = remaining / spd * 60
                    if eta_min >= 60:
                        eta_h, eta_m = divmod(int(eta_min), 60)
                        parts.append(f"{eta_h}h{eta_m:02d}m（{label}）")
                    else:
                        parts.append(f"{int(eta_min)}分（{label}）")
            self._lbl_eta.setText(
                "LvUP予測:  " + "  /  ".join(parts) if parts else "LvUP予測:  —"
            )
        else:
            self._lbl_eta.setText("LvUP予測:  —")


# ================================================================== メインウィジェット
class WatcherEditorWidget(QWidget):
    """ウォッチャー一覧と編集を提供するタブウィジェット。"""

    watchers_changed = Signal()

    def __init__(self, main_window) -> None:
        super().__init__()
        self._mw = main_window
        self._watchers: list[Watcher] = []
        self._watcher_paths: list[str] = []   # _watchers と 1:1 対応するファイルパス
        self._build_ui()
        self._load_from_dir()
        self._fire_timer = QTimer(self)
        self._fire_timer.timeout.connect(self._refresh_list)
        self._fire_timer.start(30_000)

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        hdr = QHBoxLayout()
        title = QLabel(f"グローバルウォッチャー  （保存先: {WATCHERS_DIR}/）")
        title.setStyleSheet("font-weight: bold;")
        hdr.addWidget(title, 1)
        btn_import = QPushButton("📂 インポート")
        btn_import.clicked.connect(self._import_watcher)
        hdr.addWidget(btn_import)
        lay.addLayout(hdr)

        hint = QLabel(
            "ここに登録したウォッチャーは、どのフローを実行中でも常時監視されます。\n"
            "体力低下・ポーション残量・PVP攻撃などの共通監視をここで管理してください。"
        )
        hint.setStyleSheet("color: #555; font-size: 10px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.setStyleSheet("""
            QListWidget { background: #ffffff; }
            QListWidget::item { color: #111111; padding: 2px; }
            QListWidget::item:alternate { background: #f0f4f8; }
            QListWidget::item:selected { background: #bbdefb; color: #0d0d0d; }
            QListWidget::item:selected:!active { background: #dce8f5; color: #0d0d0d; }
        """)
        lay.addWidget(self.list, 1)

        btn_row = QHBoxLayout()
        self.btn_add    = QPushButton("＋ 追加")
        self.btn_edit   = QPushButton("✎ 編集")
        self.btn_del    = QPushButton("✕ 削除")
        self.btn_up     = QPushButton("↑")
        self.btn_down   = QPushButton("↓")
        self.btn_toggle = QPushButton("有効/無効")
        for b in (self.btn_add, self.btn_edit, self.btn_del,
                  self.btn_up, self.btn_down, self.btn_toggle):
            btn_row.addWidget(b)
        btn_row.addStretch()
        self.btn_test = QPushButton("🧪 一括テスト")
        self.btn_test.setToolTip(
            "スクショ or 画像 1 枚に対して全ウォッチャーのスコアを並べる（誤発火検証）"
        )
        btn_row.addWidget(self.btn_test)
        lay.addLayout(btn_row)

        self.btn_add.clicked.connect(self._add)
        self.btn_edit.clicked.connect(self._edit)
        self.btn_del.clicked.connect(self._delete)
        self.btn_up.clicked.connect(self._move_up)
        self.btn_down.clicked.connect(self._move_down)
        self.btn_toggle.clicked.connect(self._toggle_enabled)
        self.btn_test.clicked.connect(self._run_bulk_test)
        self.list.currentRowChanged.connect(self._on_selection_changed)
        self.list.itemDoubleClicked.connect(lambda _: self._edit())
        self._on_selection_changed(-1)

        self._exp_meter = ExpMeterWidget(self._mw)
        lay.addWidget(self._exp_meter)

    # --------------------------------------------------------- ファイル操作
    def _load_from_dir(self) -> None:
        os.makedirs(WATCHERS_DIR, exist_ok=True)
        pairs = load_watchers_dir(WATCHERS_DIR)
        self._watchers = [w for _, w in pairs]
        self._watcher_paths = [p for p, _ in pairs]
        # 旧形式 watchers.json があれば移行して削除
        legacy = "watchers.json"
        if os.path.exists(legacy):
            try:
                old = load_watchers(legacy)
                for w in old:
                    if not any(x.id == w.id for x in self._watchers):
                        path = self._default_path(w)
                        save_watcher(w, path)
                        self._watchers.append(w)
                        self._watcher_paths.append(path)
                os.rename(legacy, legacy + ".migrated")
            except Exception:
                pass
        self._refresh_list()

    def _default_path(self, w: Watcher) -> str:
        """ウォッチャーのデフォルト保存パスを返す。"""
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (w.title or w.id))
        return os.path.join(WATCHERS_DIR, f"{safe}_{w.id}.json")

    def _save_one(self, idx: int) -> None:
        """1件だけ保存する。"""
        try:
            save_watcher(self._watchers[idx], self._watcher_paths[idx])
        except Exception as e:
            QMessageBox.critical(self, "保存エラー", f"保存失敗: {e}")

    def _import_watcher(self) -> None:
        """別の場所に保存されたウォッチャー JSON をインポートする。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "ウォッチャーをインポート", WATCHERS_DIR, "JSON (*.json)")
        if not path:
            return
        try:
            w = load_watcher(path)
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"読込失敗: {e}")
            return
        dest = self._default_path(w)
        try:
            save_watcher(w, dest)
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"保存失敗: {e}")
            return
        self._watchers.append(w)
        self._watcher_paths.append(dest)
        self.list.addItem(self._make_item(w, dest))
        self.list.setCurrentRow(self.list.count() - 1)

    def get_watchers(self) -> list[Watcher]:
        return list(self._watchers)

    # --------------------------------------------------------- リスト
    @staticmethod
    def _parse_today_fire_log() -> dict[str, list[str]]:
        """今日のログファイルからウォッチャータイトル → 発火時刻リストを返す。

        ログ行の形式: [HH:MM:SS] 👁 watcher 発火: [タイトル] → handler
        """
        today = _dt.now().strftime("%Y-%m-%d")
        log_path = os.path.join("logs", f"{today}.log")
        result: dict[str, list[str]] = {}
        if not os.path.exists(log_path):
            return result
        pattern = re.compile(r'^\[(\d{2}:\d{2}):\d{2}\] 👁 watcher 発火: \[(.+?)\]')
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    m = pattern.match(line)
                    if m:
                        hm, title = m.group(1), m.group(2)
                        result.setdefault(title, []).append(hm)
        except Exception:
            pass
        return result

    def _refresh_list(self) -> None:
        fire_log = self._parse_today_fire_log()
        row = self.list.currentRow()
        self.list.clear()
        for w, p in zip(self._watchers, self._watcher_paths):
            self.list.addItem(self._make_item(w, p, fire_log.get(w.title or w.id)))
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)
        self._on_selection_changed(self.list.currentRow())

    def _make_item(self, w: Watcher, path: str = "",
                   fire_times: "list[str] | None" = None) -> QListWidgetItem:
        ctype = w.condition.type
        cond_label = _COND_LABELS.get(ctype, ctype)
        after_label = _AFTER_LABELS.get(w.after, w.after)
        handler_name = (
            os.path.basename(w.handler).removesuffix(".json") if w.handler else "（なし）"
        )
        alert_icon = "  🔔" if w.alert_desktop else ""
        fname = os.path.basename(path) if path else ""
        fire_str = ""
        if fire_times:
            fire_str = f"\n      🔥 本日 {len(fire_times)}回  最終: {fire_times[-1]}"
        text = (
            f"[{'✓' if w.enabled else '✗'}]  {w.title or w.id}  |  {cond_label}{alert_icon}"
            f"\n      → {handler_name}  /  {after_label}"
            f"  /  優先度:{w.priority}  冷却:{w.cooldown_s:.0f}s"
            + (f"\n      📄 {fname}" if fname else "")
            + fire_str
        )
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, w.id)
        item.setForeground(QBrush(QColor("#999" if not w.enabled else "#111")))
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        item.setFont(font)
        return item

    def _on_selection_changed(self, row: int) -> None:
        has = row >= 0
        for b in (self.btn_edit, self.btn_del,
                  self.btn_up, self.btn_down, self.btn_toggle):
            b.setEnabled(has)

    def _require_connected(self) -> bool:
        if not self._mw.current_serial:
            QMessageBox.information(
                self, "デバイス未接続",
                "スクショ取得にはデバイスの接続が必要です。\n"
                "先にデバイスに『接続』してください。"
            )
            return False
        return True

    # --------------------------------------------------------- CRUD
    def _add(self) -> None:
        if not self._require_connected():
            return
        dlg = _WatcherWizard(serial=self._mw.current_serial, parent=self)
        if dlg.exec() == QDialog.Accepted:
            w = dlg.result_watcher()
            if w:
                path = self._default_path(w)
                self._watchers.append(w)
                self._watcher_paths.append(path)
                self._save_one(len(self._watchers) - 1)
                self.list.addItem(self._make_item(w, path))
                self.list.setCurrentRow(self.list.count() - 1)
                self.watchers_changed.emit()

    def _edit(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        if not self._require_connected():
            return
        dlg = _WatcherWizard(serial=self._mw.current_serial,
                              watcher=self._watchers[row], parent=self)
        if dlg.exec() == QDialog.Accepted:
            w = dlg.result_watcher()
            if w:
                self._watchers[row] = w
                self._save_one(row)
                self.list.takeItem(row)
                self.list.insertItem(row, self._make_item(w))
                self.list.setCurrentRow(row)
                self.watchers_changed.emit()

    def _delete(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        w = self._watchers[row]
        if QMessageBox.question(
            self, "削除確認", f"ウォッチャー「{w.title or w.id}」を削除しますか？",
            QMessageBox.Yes | QMessageBox.No
        ) == QMessageBox.Yes:
            path = self._watcher_paths[row]
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                QMessageBox.warning(self, "削除エラー", f"ファイル削除失敗: {e}")
            self._watchers.pop(row)
            self._watcher_paths.pop(row)
            self.list.takeItem(row)
            self.watchers_changed.emit()

    def _move_up(self) -> None:
        row = self.list.currentRow()
        if row <= 0:
            return
        self._watchers[row - 1], self._watchers[row] = \
            self._watchers[row], self._watchers[row - 1]
        self._watcher_paths[row - 1], self._watcher_paths[row] = \
            self._watcher_paths[row], self._watcher_paths[row - 1]
        self._refresh_list()
        self.list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self.list.currentRow()
        if row < 0 or row >= len(self._watchers) - 1:
            return
        self._watchers[row], self._watchers[row + 1] = \
            self._watchers[row + 1], self._watchers[row]
        self._watcher_paths[row], self._watcher_paths[row + 1] = \
            self._watcher_paths[row + 1], self._watcher_paths[row]
        self._refresh_list()
        self.list.setCurrentRow(row + 1)

    def _toggle_enabled(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        w = self._watchers[row]
        w.enabled = not w.enabled
        self._save_one(row)
        self.list.takeItem(row)
        self.list.insertItem(row, self._make_item(w))
        self.list.setCurrentRow(row)
        self.watchers_changed.emit()

    def _run_bulk_test(self) -> None:
        if not self._watchers:
            QMessageBox.information(
                self, "情報", "テストするウォッチャーがありません"
            )
            return
        dlg = WatcherTestDialog(
            watchers=self._watchers,
            serial=self._mw.current_serial,
            parent=self,
        )
        dlg.exec()

    def toggle_watcher_by_id(self, watcher_id: str, enabled: bool) -> None:
        """フローエディタのタグからの有効/無効切替。"""
        for i, w in enumerate(self._watchers):
            if w.id == watcher_id:
                w.enabled = enabled
                self._save_one(i)
                self.list.takeItem(i)
                self.list.insertItem(i, self._make_item(w, self._watcher_paths[i]))
                break
