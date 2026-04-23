"""ウォッチャー編集タブ。

watchers.json をフローとは独立して管理する。
新規作成はスクショベースのウィザード形式：
  ① タイトル入力 + スクショ取得 + 範囲ドラッグ選択
  ② 検知条件（画像出現/消滅/OCR数値）+ アクション設定
"""
from __future__ import annotations

import os
import uuid

import cv2
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QRadioButton, QSpinBox, QStackedWidget, QVBoxLayout,
    QWidget,
)

from .flow import Condition, Watcher, load_watchers, save_watchers
from .ocr_test_dialog import ImageCanvas

WATCHERS_PATH = "watchers.json"
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
    "stop":          "フローを停止する",
}


# ------------------------------------------------------------------ ユーティリティ
def _np_to_pixmap(img: np.ndarray, max_w: int = 300, max_h: int = 120) -> QPixmap:
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    qimg = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888)
    pix = QPixmap.fromImage(qimg)
    return pix.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)


# ================================================================== ウィザード
class _WatcherWizard(QDialog):
    """スクショベースのウォッチャー作成/編集ウィザード（2ページ）。"""

    def __init__(self, serial: str | None = None,
                 watcher: Watcher | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ウォッチャー設定")
        self.setMinimumSize(860, 660)

        self._serial = serial
        self._edit_watcher = watcher   # 編集時の元データ
        self._img: np.ndarray | None = None      # フルスクショ
        self._crop: np.ndarray | None = None     # 選択範囲の切り抜き
        self._region: list[int] = []             # [x, y, w, h]
        self._result: Watcher | None = None

        root = QVBoxLayout(self)
        self._stack = QStackedWidget()
        root.addWidget(self._stack, 1)

        self._page0 = self._build_page0()
        self._page1 = self._build_page1()
        self._stack.addWidget(self._page0)
        self._stack.addWidget(self._page1)

        if watcher:
            self._prefill(watcher)

    # =========================================================== ページ0
    def _build_page0(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(6)

        lay.addWidget(QLabel("① タイトルを入力し、スマホ画面をキャプチャして監視したい箇所をドラッグで選択してください"))

        # タイトル
        form = QFormLayout()
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("例: ポーション低下、体力ピンチ、PVP攻撃")
        self.title_edit.textChanged.connect(self._update_next_btn)
        form.addRow("タイトル (必須):", self.title_edit)
        lay.addLayout(form)

        # キャプチャボタン
        cap_row = QHBoxLayout()
        btn_cap = QPushButton("📷 スクショ取得（接続中デバイス）")
        btn_cap.clicked.connect(self._capture)
        btn_file = QPushButton("📂 ファイルから開く")
        btn_file.clicked.connect(self._open_file)
        cap_row.addWidget(btn_cap)
        cap_row.addWidget(btn_file)
        cap_row.addStretch()
        lay.addLayout(cap_row)

        self._hint0 = QLabel("← スクショを取得後、監視したい箇所をドラッグで囲んでください")
        self._hint0.setStyleSheet("color: #777; font-size: 10px;")
        lay.addWidget(self._hint0)

        # キャンバス
        self._canvas = ImageCanvas()
        self._canvas.region_selected.connect(self._on_region_selected)
        lay.addWidget(self._canvas, 1)

        # 切り抜きプレビュー
        prev_row = QHBoxLayout()
        prev_row.addWidget(QLabel("選択範囲:"))
        self._crop_label0 = QLabel("（未選択）")
        self._crop_label0.setMinimumHeight(60)
        self._crop_label0.setStyleSheet("border:1px solid #aaa; background:#111;")
        self._crop_label0.setAlignment(Qt.AlignCenter)
        prev_row.addWidget(self._crop_label0, 1)
        lay.addLayout(prev_row)

        # ナビ
        nav = QHBoxLayout()
        nav.addStretch()
        self._btn_next = QPushButton("次へ →")
        self._btn_next.setEnabled(False)
        self._btn_next.setFixedWidth(120)
        self._btn_next.clicked.connect(self._go_page1)
        nav.addWidget(self._btn_next)
        lay.addLayout(nav)

        return page

    # =========================================================== ページ1
    def _build_page1(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(6)

        lay.addWidget(QLabel("② 検知方法とアクションを設定してください"))

        top = QHBoxLayout()

        # 左: 切り抜き大表示
        left = QVBoxLayout()
        left.addWidget(QLabel("選択した範囲:"))
        self._crop_label1 = QLabel()
        self._crop_label1.setFixedSize(260, 160)
        self._crop_label1.setStyleSheet("border:1px solid #aaa; background:#111;")
        self._crop_label1.setAlignment(Qt.AlignCenter)
        left.addWidget(self._crop_label1)
        left.addStretch()
        top.addLayout(left)

        # 右: 条件 + アクション
        right = QVBoxLayout()

        # 検知方法ラジオ
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
        right.addWidget(grp_type)

        # 条件設定（スタック）
        grp_cond = QGroupBox("条件の詳細")
        cond_lay = QVBoxLayout(grp_cond)
        self._cond_stack = QStackedWidget()

        # image_appear ページ
        p_appear = QWidget()
        f_appear = QFormLayout(p_appear)
        self.threshold_appear = QDoubleSpinBox()
        self.threshold_appear.setRange(0.0, 1.0); self.threshold_appear.setSingleStep(0.01)
        self.threshold_appear.setValue(0.85); self.threshold_appear.setDecimals(2)
        f_appear.addRow("マッチ閾値 (0〜1):", self.threshold_appear)
        hint_appear = QLabel("選択範囲がこの画像と一致したときに発火します")
        hint_appear.setStyleSheet("color:#555; font-size:9px;"); hint_appear.setWordWrap(True)
        f_appear.addRow("", hint_appear)
        self._cond_stack.addWidget(p_appear)

        # image_gone ページ
        p_gone = QWidget()
        f_gone = QFormLayout(p_gone)
        self.threshold_gone = QDoubleSpinBox()
        self.threshold_gone.setRange(0.0, 1.0); self.threshold_gone.setSingleStep(0.01)
        self.threshold_gone.setValue(0.85); self.threshold_gone.setDecimals(2)
        f_gone.addRow("マッチ閾値 (0〜1):", self.threshold_gone)
        self.consecutive = QSpinBox()
        self.consecutive.setRange(1, 30); self.consecutive.setValue(3)
        f_gone.addRow("連続ミス回数:", self.consecutive)
        hint_gone = QLabel("選択範囲の画像がN回連続して検出されなくなったときに発火します")
        hint_gone.setStyleSheet("color:#555; font-size:9px;"); hint_gone.setWordWrap(True)
        f_gone.addRow("", hint_gone)
        self._cond_stack.addWidget(p_gone)

        # ocr_number ページ
        p_ocr = QWidget()
        f_ocr = QFormLayout(p_ocr)
        self.ocr_whitelist = QLineEdit("0123456789")
        f_ocr.addRow("読み取る文字種:", self.ocr_whitelist)
        op_row = QHBoxLayout()
        self.ocr_op = QComboBox()
        for op in ("<", "<=", ">", ">=", "=="):
            self.ocr_op.addItem(op, op)
        self.ocr_op.setCurrentIndex(1)  # "<=" default
        op_row.addWidget(self.ocr_op)
        self.ocr_value = QSpinBox(); self.ocr_value.setRange(0, 99999)
        op_row.addWidget(self.ocr_value); op_row.addStretch()
        f_ocr.addRow("発火条件 (数値):", op_row)
        btn_ocr_test = QPushButton("▶ OCRテスト（切り抜き範囲で数値を確認）")
        btn_ocr_test.clicked.connect(self._run_ocr_test)
        f_ocr.addRow("", btn_ocr_test)
        self._ocr_result_lbl = QLabel("（テスト未実行）")
        self._ocr_result_lbl.setStyleSheet("font-weight:bold; color:#1565c0;")
        f_ocr.addRow("OCR結果:", self._ocr_result_lbl)
        self._cond_stack.addWidget(p_ocr)

        cond_lay.addWidget(self._cond_stack)
        right.addWidget(grp_cond)

        # アクション設定
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
        act_lay.addRow("完了後:", self.after_combo)
        self.cooldown_spin = QDoubleSpinBox()
        self.cooldown_spin.setRange(0, 3600); self.cooldown_spin.setSingleStep(1.0)
        self.cooldown_spin.setSuffix(" 秒")
        act_lay.addRow("クールダウン:", self.cooldown_spin)
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(0, 999)
        act_lay.addRow("優先度 (大きいほど優先):", self.priority_spin)
        self.enabled_check = QCheckBox("有効")
        self.enabled_check.setChecked(True)
        act_lay.addRow("", self.enabled_check)
        right.addWidget(grp_act)

        top.addLayout(right, 1)
        lay.addLayout(top, 1)

        # ナビ
        nav = QHBoxLayout()
        btn_back = QPushButton("← 戻る")
        btn_back.clicked.connect(lambda: self._stack.setCurrentIndex(0))
        self._btn_ok = QPushButton("✓ 確定")
        self._btn_ok.setStyleSheet(
            "QPushButton{background:#1565c0;color:white;font-weight:bold;padding:6px;}"
            "QPushButton:hover{background:#0d47a1;}"
        )
        self._btn_ok.clicked.connect(self._on_ok)
        btn_cancel = QPushButton("キャンセル")
        btn_cancel.clicked.connect(self.reject)
        nav.addWidget(btn_back)
        nav.addStretch()
        nav.addWidget(btn_cancel)
        nav.addWidget(self._btn_ok)
        lay.addLayout(nav)

        return page

    # =========================================================== ページ0 ロジック
    def _capture(self) -> None:
        if not self._serial:
            QMessageBox.information(self, "情報",
                "デバイスが接続されていません。\n"
                "メイン画面でデバイスに接続してから実行してください。")
            return
        try:
            from .adb import screencap
            self._hint0.setText("取得中…")
            self.repaint()
            png = screencap(self._serial)
            arr = np.frombuffer(png, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("デコード失敗")
            self._img = img
            self._canvas.set_image(img)
            self._hint0.setText("監視したい箇所をドラッグで囲んでください")
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"スクショ取得失敗:\n{e}")

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
        self._hint0.setText("監視したい箇所をドラッグで囲んでください")

    def _on_region_selected(self, x: int, y: int, w: int, h: int) -> None:
        self._region = [x, y, w, h]
        self._update_crop()
        self._update_next_btn()

    def _update_crop(self) -> None:
        if self._img is None or not self._region:
            return
        x, y, w, h = self._region
        ih, iw = self._img.shape[:2]
        crop = self._img[max(0,y):min(y+h,ih), max(0,x):min(x+w,iw)]
        if crop.size == 0:
            return
        self._crop = crop.copy()
        pix = _np_to_pixmap(crop, 400, 80)
        self._crop_label0.setPixmap(pix)
        self._crop_label0.setText("")

    def _update_next_btn(self) -> None:
        ok = bool(self.title_edit.text().strip()) and bool(self._region)
        self._btn_next.setEnabled(ok)

    def _go_page1(self) -> None:
        if self._crop is not None:
            pix = _np_to_pixmap(self._crop, 260, 160)
            self._crop_label1.setPixmap(pix)
            self._crop_label1.setText("")
        self._stack.setCurrentIndex(1)

    # =========================================================== ページ1 ロジック
    def _on_type_changed(self, idx: int) -> None:
        self._cond_stack.setCurrentIndex(idx)

    def _browse_handler(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "ハンドラシーン選択", SCENES_DIR, "JSON (*.json)")
        if path:
            rel = os.path.relpath(path, SCENES_DIR).replace("\\", "/")
            self.handler_edit.setText(rel)

    def _run_ocr_test(self) -> None:
        if self._crop is None:
            QMessageBox.information(self, "情報", "ページ1でスクショ範囲を選択してください")
            return
        try:
            import pytesseract
        except ImportError:
            QMessageBox.warning(self, "未インストール",
                "pytesseract がインストールされていません。\n"
                "pip install pytesseract を実行し、\n"
                "Tesseract-OCR もインストールしてください。")
            return
        wl = self.ocr_whitelist.text().strip()
        config = "--psm 7 --oem 3"
        if wl:
            config += f" -c tessedit_char_whitelist={wl}"
        try:
            gray = cv2.cvtColor(self._crop, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
            _, gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            text = pytesseract.image_to_string(gray, config=config).strip()
            if text:
                self._ocr_result_lbl.setText(text)
                self._ocr_result_lbl.setStyleSheet("font-weight:bold; color:#1b5e20; font-size:16px;")
            else:
                self._ocr_result_lbl.setText("読み取れませんでした — 範囲や文字種を変更してみてください")
                self._ocr_result_lbl.setStyleSheet("font-weight:bold; color:#c62828;")
        except Exception as e:
            self._ocr_result_lbl.setText(f"エラー: {e}")

    # =========================================================== 確定・読込
    def _on_ok(self) -> None:
        ctype_idx = self._type_group.checkedId()
        ctype = ["image_appear", "image_gone", "ocr_number"][ctype_idx]

        template_path = ""
        if ctype in ("image_appear", "image_gone"):
            # 切り抜き画像をテンプレートとして保存
            if self._crop is None:
                QMessageBox.warning(self, "エラー", "スクショ範囲が選択されていません")
                return
            os.makedirs(TEMPLATES_DIR, exist_ok=True)
            wid = (self._edit_watcher.id if self._edit_watcher else str(uuid.uuid4())[:8])
            fname = f"{wid}_{ctype}.png"
            template_path = os.path.join(TEMPLATES_DIR, fname).replace("\\", "/")
            cv2.imwrite(template_path, self._crop)

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
                             value=self.ocr_value.value())

        wid = (self._edit_watcher.id if self._edit_watcher else str(uuid.uuid4())[:8])
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
        )
        self.accept()

    def _prefill(self, w: Watcher) -> None:
        """編集時: 既存ウォッチャーの値をフォームに読み込む。"""
        self.title_edit.setText(w.title)
        self.enabled_check.setChecked(w.enabled)
        self.priority_spin.setValue(w.priority)
        self.handler_edit.setText(w.handler)
        idx = self.after_combo.findData(w.after)
        if idx >= 0:
            self.after_combo.setCurrentIndex(idx)
        self.cooldown_spin.setValue(w.cooldown_s)

        ctype = w.condition.type
        if ctype == "image_appear":
            self._rb_appear.setChecked(True)
            self._cond_stack.setCurrentIndex(0)
            self.threshold_appear.setValue(w.condition.threshold)
        elif ctype == "image_gone":
            self._rb_gone.setChecked(True)
            self._cond_stack.setCurrentIndex(1)
            self.threshold_gone.setValue(w.condition.threshold)
            self.consecutive.setValue(w.condition.consecutive)
        elif ctype == "ocr_number":
            self._rb_ocr.setChecked(True)
            self._cond_stack.setCurrentIndex(2)
            self.ocr_whitelist.setText(w.condition.ocr_whitelist)
            idx2 = self.ocr_op.findData(w.condition.op)
            if idx2 >= 0:
                self.ocr_op.setCurrentIndex(idx2)
            self.ocr_value.setValue(w.condition.value)

        self._region = list(w.condition.region) if w.condition.region else []

        # 既存テンプレート画像があれば表示
        if ctype in ("image_appear", "image_gone") and w.condition.template:
            img = cv2.imread(w.condition.template, cv2.IMREAD_COLOR)
            if img is not None:
                self._crop = img
                self._img = img
                self._canvas.set_image(img)
                pix = _np_to_pixmap(img, 400, 80)
                self._crop_label0.setPixmap(pix)
                self._crop_label0.setText("")
                pix1 = _np_to_pixmap(img, 260, 160)
                self._crop_label1.setPixmap(pix1)
                self._crop_label1.setText("")
        elif ctype == "ocr_number" and self._region:
            self._update_next_btn()

    def result_watcher(self) -> Watcher | None:
        return self._result


# ================================================================== メインウィジェット
class WatcherEditorWidget(QWidget):
    """ウォッチャー一覧と編集を提供するタブウィジェット。"""

    def __init__(self, main_window) -> None:
        super().__init__()
        self._mw = main_window
        self._watchers: list[Watcher] = []
        self._build_ui()
        self._load_from_file()

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setSpacing(6)

        hdr = QHBoxLayout()
        title = QLabel(f"グローバルウォッチャー  （保存先: {WATCHERS_PATH}）")
        title.setStyleSheet("font-weight: bold;")
        hdr.addWidget(title, 1)
        self.btn_save = QPushButton("💾 保存")
        self.btn_save.clicked.connect(self._save)
        hdr.addWidget(self.btn_save)
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
        lay.addLayout(btn_row)

        self.btn_add.clicked.connect(self._add)
        self.btn_edit.clicked.connect(self._edit)
        self.btn_del.clicked.connect(self._delete)
        self.btn_up.clicked.connect(self._move_up)
        self.btn_down.clicked.connect(self._move_down)
        self.btn_toggle.clicked.connect(self._toggle_enabled)
        self.list.currentRowChanged.connect(self._on_selection_changed)
        self.list.itemDoubleClicked.connect(lambda _: self._edit())
        self._on_selection_changed(-1)

    # --------------------------------------------------------- ファイル操作
    def _load_from_file(self) -> None:
        try:
            self._watchers = load_watchers(WATCHERS_PATH)
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"ウォッチャー読込失敗: {e}")
            self._watchers = []
        self._refresh_list()

    def _save(self) -> None:
        try:
            save_watchers(self._watchers, WATCHERS_PATH)
            QMessageBox.information(self, "保存完了",
                                    f"ウォッチャーを保存しました\n({WATCHERS_PATH})")
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"保存失敗: {e}")

    def get_watchers(self) -> list[Watcher]:
        return list(self._watchers)

    # --------------------------------------------------------- リスト
    def _refresh_list(self) -> None:
        row = self.list.currentRow()
        self.list.clear()
        for w in self._watchers:
            self.list.addItem(self._make_item(w))
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)
        self._on_selection_changed(self.list.currentRow())

    def _make_item(self, w: Watcher) -> QListWidgetItem:
        ctype = w.condition.type
        cond_label = _COND_LABELS.get(ctype, ctype)
        after_label = _AFTER_LABELS.get(w.after, w.after)
        handler_name = (
            os.path.basename(w.handler).removesuffix(".json") if w.handler else "（なし）"
        )
        text = (
            f"[{'✓' if w.enabled else '✗'}]  {w.title or w.id}  |  {cond_label}"
            f"\n      → {handler_name}  /  {after_label}"
            f"  /  優先度:{w.priority}  冷却:{w.cooldown_s:.0f}s"
        )
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, w.id)
        item.setForeground(QBrush(QColor("#aaa" if not w.enabled else "#111")))
        font = QFont(); font.setPointSize(9)
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
                self._watchers.append(w)
                self.list.addItem(self._make_item(w))
                self.list.setCurrentRow(self.list.count() - 1)

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
                self.list.takeItem(row)
                self.list.insertItem(row, self._make_item(w))
                self.list.setCurrentRow(row)

    def _delete(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        w = self._watchers[row]
        if QMessageBox.question(
            self, "削除確認", f"ウォッチャー「{w.title or w.id}」を削除しますか？",
            QMessageBox.Yes | QMessageBox.No
        ) == QMessageBox.Yes:
            self._watchers.pop(row)
            self.list.takeItem(row)

    def _move_up(self) -> None:
        row = self.list.currentRow()
        if row <= 0:
            return
        self._watchers[row - 1], self._watchers[row] = \
            self._watchers[row], self._watchers[row - 1]
        self._refresh_list()
        self.list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self.list.currentRow()
        if row < 0 or row >= len(self._watchers) - 1:
            return
        self._watchers[row], self._watchers[row + 1] = \
            self._watchers[row + 1], self._watchers[row]
        self._refresh_list()
        self.list.setCurrentRow(row + 1)

    def _toggle_enabled(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        w = self._watchers[row]
        w.enabled = not w.enabled
        self.list.takeItem(row)
        self.list.insertItem(row, self._make_item(w))
        self.list.setCurrentRow(row)
