"""ウォッチャー編集タブ。

watchers.json をフローとは独立して管理する。
新規作成/編集はスクショベースの1画面ダイアログ：
  左: スクショキャンバス（ズーム/パン/範囲選択）
  右: 検知方法 + 条件詳細 + アクション設定
"""
from __future__ import annotations

import os
import uuid

import cv2
import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QRadioButton, QSpinBox, QSplitter, QStackedWidget, QVBoxLayout,
    QWidget,
)

from .flow import (Condition, Watcher,
                   load_watcher, save_watcher, load_watchers_dir,
                   load_watchers)   # load_watchers は旧形式移行用
from .ocr_test_dialog import ImageCanvas

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
        self.alert_check = QCheckBox("🔔 発火時にデスクトップ通知を表示する")
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
        self._load_screenshot()

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
                self._canvas.highlight_region(*self._region)
                self._run_ocr_test()
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
        # OCR タイプなら即座にテスト実行
        if self._type_group.checkedId() == 2:
            self._run_ocr_test()

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
                if self._region:
                    self._canvas.highlight_region(*self._region)
                pix = _np_to_pixmap(img, 600, 64)
                self._crop_label.setPixmap(pix)
                self._crop_label.setText("")

    def result_watcher(self) -> Watcher | None:
        return self._result


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
    def _refresh_list(self) -> None:
        row = self.list.currentRow()
        self.list.clear()
        for w, p in zip(self._watchers, self._watcher_paths):
            self.list.addItem(self._make_item(w, p))
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)
        self._on_selection_changed(self.list.currentRow())

    def _make_item(self, w: Watcher, path: str = "") -> QListWidgetItem:
        ctype = w.condition.type
        cond_label = _COND_LABELS.get(ctype, ctype)
        after_label = _AFTER_LABELS.get(w.after, w.after)
        handler_name = (
            os.path.basename(w.handler).removesuffix(".json") if w.handler else "（なし）"
        )
        alert_icon = "  🔔" if w.alert_desktop else ""
        fname = os.path.basename(path) if path else ""
        text = (
            f"[{'✓' if w.enabled else '✗'}]  {w.title or w.id}  |  {cond_label}{alert_icon}"
            f"\n      → {handler_name}  /  {after_label}"
            f"  /  優先度:{w.priority}  冷却:{w.cooldown_s:.0f}s"
            + (f"\n      📄 {fname}" if fname else "")
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

    def toggle_watcher_by_id(self, watcher_id: str, enabled: bool) -> None:
        """フローエディタのタグからの有効/無効切替。"""
        for i, w in enumerate(self._watchers):
            if w.id == watcher_id:
                w.enabled = enabled
                self._save_one(i)
                self.list.takeItem(i)
                self.list.insertItem(i, self._make_item(w, self._watcher_paths[i]))
                break
