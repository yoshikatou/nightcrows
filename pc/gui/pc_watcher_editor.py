"""PC ウォッチャー編集ウィンドウ（独立、約 1000x800）。

ワークフロー:
    1. スクショ取得 → snapshots/ に保存し画像表示
    2. 画像上をドラッグして検知対象の領域を選択
       → 領域を watcher_templates/<watcher_id>.png として切り出し保存
       → 領域比率を condition.region に保存
    3. 閾値・優先度・冷却時間などを設定
    4. 「現在のスクショで判定」で1回テスト
    5. 「連続監視テスト」で実機動作確認（発火するまで監視ループ）
    6. 「保存」で watchers/<title>_<id>.json に書き出し
"""
from __future__ import annotations

import os
import random
import threading
import time
from datetime import datetime

import cv2
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .capture import capture_window
from .logger import write_log
from .pc_canvas import PcSnapshotCanvas, RegionMarker
from .pc_scene import SCENES_DIR
from .pc_watcher import (
    OPS,
    WATCHER_TEMPLATES_DIR,
    WATCHERS_DIR,
    EvalResult,
    PcWatcher,
    WatcherCondition,
    evaluate_watcher,
    load_pc_watcher,
    save_pc_watcher,
)
from .window_picker import find_hwnd_by_title

SNAPSHOTS_DIR = "snapshots"


class WatcherEditorWindow(QWidget):
    """ウォッチャー編集ウィンドウ（独立、約 1000x800）。"""

    _log_signal = Signal(str)
    _watch_state_signal = Signal(bool)   # True=監視中, False=停止
    saved  = Signal(str)                 # 保存完了 (パス) — メインの一覧更新トリガ
    closed = Signal(object)              # ウィンドウクローズ通知 — 参照解除用

    def __init__(
        self,
        watcher_path: str | None,
        window_title: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(None)
        self.setWindowTitle("ウォッチャー編集")
        self.resize(1000, 800)

        if watcher_path and os.path.exists(watcher_path):
            self._watcher = load_pc_watcher(watcher_path)
            self._path: str | None = watcher_path
        else:
            self._watcher = PcWatcher(title="新規ウォッチャー")
            self._path = watcher_path

        self._window_title = window_title

        self._current_snapshot_path: str | None = None
        self._watch_thread: threading.Thread | None = None
        self._stop_flag = False

        self._log_signal.connect(self._append_log)
        self._watch_state_signal.connect(self._on_watch_state)

        self._build_ui()
        self._reload_template_preview()

    # ----------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # ヘッダー
        head = QHBoxLayout()
        head.addWidget(QLabel("タイトル:"))
        self._inp_title = QLineEdit(self._watcher.title)
        self._inp_title.editingFinished.connect(self._on_title_changed)
        head.addWidget(self._inp_title, 1)
        head.addSpacing(12)
        head.addWidget(QLabel("対象ウィンドウ:"))
        self._lbl_win = QLabel(self._window_title or "(未設定)")
        head.addWidget(self._lbl_win)
        head.addStretch(1)
        btn_save = QPushButton("保存")
        btn_save.setFixedWidth(80)
        btn_save.clicked.connect(self._save)
        head.addWidget(btn_save)
        outer.addLayout(head)

        # Splitter: 左 = キャンバス、右 = 設定
        split = QSplitter(Qt.Horizontal)

        self._canvas = PcSnapshotCanvas()
        self._canvas.region_selected.connect(self._on_canvas_region)
        split.addWidget(self._canvas)

        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(4, 0, 0, 0)

        # スクショ取得
        rlay.addWidget(QLabel("画像:"))
        btn_cap = QPushButton("スクショ取得")
        btn_cap.clicked.connect(self._capture_snapshot)
        rlay.addWidget(btn_cap)
        rlay.addSpacing(6)

        # 検知タイプ
        rlay.addWidget(QLabel("検知タイプ:"))
        self._cmb_type = QComboBox()
        self._cmb_type.addItem("image_appear (画像が出現)", "image_appear")
        self._cmb_type.addItem("image_gone   (画像が消えた)", "image_gone")
        self._cmb_type.addItem("ocr_number   (OCR 数値判定)", "ocr_number")
        idx = self._cmb_type.findData(self._watcher.condition.type)
        if idx >= 0:
            self._cmb_type.setCurrentIndex(idx)
        self._cmb_type.currentIndexChanged.connect(self._on_type_changed)
        rlay.addWidget(self._cmb_type)

        # 設定パネルを検知タイプで切り替え
        self._cond_stack = QStackedWidget()
        # image_appear ページ
        page_img = QWidget()
        img_form = QFormLayout(page_img)
        img_form.setContentsMargins(0, 0, 0, 0)
        self._spin_threshold = QDoubleSpinBox()
        self._spin_threshold.setRange(0.50, 1.00)
        self._spin_threshold.setSingleStep(0.01)
        self._spin_threshold.setDecimals(2)
        self._spin_threshold.setValue(self._watcher.condition.threshold)
        img_form.addRow("閾値:", self._spin_threshold)
        self._cond_stack.addWidget(page_img)

        # ocr_number ページ
        page_ocr = QWidget()
        ocr_form = QFormLayout(page_ocr)
        ocr_form.setContentsMargins(0, 0, 0, 0)
        self._inp_whitelist = QLineEdit(self._watcher.condition.ocr_whitelist)
        self._inp_whitelist.setPlaceholderText("0123456789")
        ocr_form.addRow("文字種:", self._inp_whitelist)
        self._cmb_op = QComboBox()
        for k in ("<", "<=", "==", "!=", ">=", ">"):
            self._cmb_op.addItem(k, k)
        op_idx = self._cmb_op.findData(self._watcher.condition.op)
        if op_idx >= 0:
            self._cmb_op.setCurrentIndex(op_idx)
        ocr_form.addRow("演算子:", self._cmb_op)
        self._spin_value = QDoubleSpinBox()
        self._spin_value.setRange(-1e9, 1e9)
        self._spin_value.setDecimals(2)
        self._spin_value.setValue(float(self._watcher.condition.value))
        ocr_form.addRow("値:", self._spin_value)
        self._spin_consecutive = QSpinBox()
        self._spin_consecutive.setRange(1, 100)
        self._spin_consecutive.setValue(int(self._watcher.condition.consecutive))
        ocr_form.addRow("連続回数:", self._spin_consecutive)
        self._cond_stack.addWidget(page_ocr)

        rlay.addWidget(self._cond_stack)
        self._on_type_changed()

        # 領域表示
        self._lbl_region = QLabel(self._region_label())
        self._lbl_region.setWordWrap(True)
        self._lbl_region.setStyleSheet("color:#555; font-size:11px;")
        rlay.addWidget(self._lbl_region)
        rlay.addSpacing(6)

        # アクション設定
        rlay.addWidget(QLabel("アクション設定:"))

        # ハンドラーシーン（発火時に実行されるシーン）
        handler_row = QHBoxLayout()
        handler_row.addWidget(QLabel("ハンドラー:"))
        self._cmb_handler = QComboBox()
        self._cmb_handler.addItem("(なし — 通知のみ)", "")
        if os.path.isdir(SCENES_DIR):
            for fname in sorted(os.listdir(SCENES_DIR)):
                if fname.endswith(".json"):
                    self._cmb_handler.addItem(fname, fname)
        idx_h = self._cmb_handler.findData(self._watcher.handler)
        if idx_h >= 0:
            self._cmb_handler.setCurrentIndex(idx_h)
        handler_row.addWidget(self._cmb_handler, 1)
        rlay.addLayout(handler_row)

        # 完了後動作
        after_row = QHBoxLayout()
        after_row.addWidget(QLabel("完了後:"))
        self._cmb_after = QComboBox()
        self._cmb_after.addItem("noop          (何もしない)", "noop")
        self._cmb_after.addItem("restart_scene (元シーンを再開)", "restart_scene")
        self._cmb_after.addItem("next_scene    (次シーンへ)", "next_scene")
        self._cmb_after.addItem("stop          (フロー停止)", "stop")
        idx_a = self._cmb_after.findData(self._watcher.after)
        if idx_a >= 0:
            self._cmb_after.setCurrentIndex(idx_a)
        after_row.addWidget(self._cmb_after, 1)
        rlay.addLayout(after_row)

        prio_row = QHBoxLayout()
        prio_row.addWidget(QLabel("優先度:"))
        self._spin_priority = QSpinBox()
        self._spin_priority.setRange(0, 1000)
        self._spin_priority.setValue(self._watcher.priority)
        prio_row.addWidget(self._spin_priority)
        prio_row.addSpacing(8)
        prio_row.addWidget(QLabel("冷却(秒):"))
        self._spin_cooldown = QDoubleSpinBox()
        self._spin_cooldown.setRange(0.0, 3600.0)
        self._spin_cooldown.setSingleStep(1.0)
        self._spin_cooldown.setDecimals(1)
        self._spin_cooldown.setValue(self._watcher.cooldown_s)
        prio_row.addWidget(self._spin_cooldown)
        prio_row.addStretch(1)
        rlay.addLayout(prio_row)

        poll_row = QHBoxLayout()
        poll_row.addWidget(QLabel("ポーリング (秒):"))
        self._spin_poll_min = QDoubleSpinBox()
        self._spin_poll_min.setRange(0.1, 60.0)
        self._spin_poll_min.setSingleStep(0.1)
        self._spin_poll_min.setDecimals(1)
        self._spin_poll_min.setValue(max(0.1, self._watcher.poll_min_s or 1.0))
        poll_row.addWidget(self._spin_poll_min)
        poll_row.addWidget(QLabel("〜"))
        self._spin_poll_max = QDoubleSpinBox()
        self._spin_poll_max.setRange(0.1, 60.0)
        self._spin_poll_max.setSingleStep(0.1)
        self._spin_poll_max.setDecimals(1)
        self._spin_poll_max.setValue(max(0.1, self._watcher.poll_max_s or 4.0))
        poll_row.addWidget(self._spin_poll_max)
        poll_row.addStretch(1)
        rlay.addLayout(poll_row)

        opt_row = QHBoxLayout()
        self._chk_enabled = QCheckBox("有効")
        self._chk_enabled.setChecked(self._watcher.enabled)
        opt_row.addWidget(self._chk_enabled)
        self._chk_alert = QCheckBox("デスクトップ通知")
        self._chk_alert.setChecked(self._watcher.alert_desktop)
        opt_row.addWidget(self._chk_alert)
        opt_row.addStretch(1)
        rlay.addLayout(opt_row)

        # テスト
        rlay.addSpacing(6)
        rlay.addWidget(QLabel("テスト:"))
        test_row = QHBoxLayout()
        self._btn_once = QPushButton("現在のスクショで判定")
        self._btn_once.clicked.connect(self._test_once)
        self._btn_watch = QPushButton("▶ 監視開始")
        self._btn_watch.clicked.connect(self._toggle_watch)
        test_row.addWidget(self._btn_once)
        test_row.addWidget(self._btn_watch)
        rlay.addLayout(test_row)

        rlay.addWidget(QLabel("ログ:"))
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet(
            "font-family: Consolas, monospace; font-size:11px;"
        )
        rlay.addWidget(self._log, 1)

        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([700, 300])
        outer.addWidget(split, 1)

    def _region_label(self) -> str:
        rg = self._watcher.condition.region
        if not rg:
            return "領域: 未設定（画像全体）"
        return (
            f"領域: rx={rg[0]:.3f} ry={rg[1]:.3f} "
            f"rw={rg[2]:.3f} rh={rg[3]:.3f}"
        )

    def _on_title_changed(self) -> None:
        t = self._inp_title.text().strip()
        if t:
            self._watcher.title = t

    def _on_type_changed(self) -> None:
        # image_appear と image_gone は同じ閾値パネルを共用、ocr_number は別
        t = self._cmb_type.currentData() or "image_appear"
        self._cond_stack.setCurrentIndex(1 if t == "ocr_number" else 0)

    # ----------------------------------------------------------------- スクショ
    def _capture_snapshot(self) -> None:
        if not self._window_title:
            QMessageBox.warning(self, "エラー", "対象ウィンドウが未設定です")
            return
        hwnd = find_hwnd_by_title(self._window_title)
        if not hwnd:
            QMessageBox.warning(
                self, "エラー",
                f"ウィンドウが見つかりません: {self._window_title}",
            )
            return
        img = capture_window(hwnd)
        if img is None:
            QMessageBox.warning(self, "エラー", "キャプチャに失敗しました")
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
        path = os.path.join(SNAPSHOTS_DIR, f"snap_{ts}.png").replace("\\", "/")
        cv2.imwrite(path, img)
        self._current_snapshot_path = path
        self._canvas.set_snapshot(path)
        self._reload_template_preview()
        self._append_log(f"スクショ取得: {path}")

    # --------------------------------------------------- ドラッグ → 領域選択
    def _on_canvas_region(self, rx: float, ry: float, rw: float, rh: float) -> None:
        if self._current_snapshot_path is None:
            QMessageBox.warning(self, "エラー", "先にスクショを取得してください")
            return
        img = cv2.imread(self._current_snapshot_path)
        if img is None:
            QMessageBox.warning(self, "エラー", "スナップ画像が読み込めません")
            return
        ih, iw = img.shape[:2]
        x0 = max(0, int(rx * iw))
        y0 = max(0, int(ry * ih))
        x1 = min(iw, int((rx + rw) * iw))
        y1 = min(ih, int((ry + rh) * ih))
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            QMessageBox.warning(self, "エラー", "領域が小さすぎます")
            return

        # テンプレ画像を watcher_templates/<id>.png として保存
        os.makedirs(WATCHER_TEMPLATES_DIR, exist_ok=True)
        tpl_path = os.path.join(
            WATCHER_TEMPLATES_DIR, f"{self._watcher.id}.png",
        ).replace("\\", "/")
        cv2.imwrite(tpl_path, crop)
        self._watcher.condition.template = tpl_path
        self._watcher.condition.region = [
            round(rx, 4), round(ry, 4), round(rw, 4), round(rh, 4),
        ]
        self._lbl_region.setText(self._region_label())
        self._reload_template_preview()
        self._append_log(
            f"領域設定: {self._region_label()}  テンプレ: {tpl_path}"
        )

    def _reload_template_preview(self) -> None:
        rg = self._watcher.condition.region
        if rg and len(rg) == 4:
            self._canvas.set_markers(regions=[RegionMarker(
                rx=float(rg[0]), ry=float(rg[1]),
                rw=float(rg[2]), rh=float(rg[3]),
                label="watcher",
            )])
        else:
            self._canvas.set_markers(regions=[])

    # ---------------------------------------------------------------- 単発テスト
    def _sync_from_ui(self) -> None:
        """UI の現在値を self._watcher に反映する。"""
        self._on_title_changed()
        c = self._watcher.condition
        c.type = self._cmb_type.currentData() or "image_appear"
        c.threshold = float(self._spin_threshold.value())
        c.ocr_whitelist = self._inp_whitelist.text().strip() or "0123456789"
        c.op = self._cmb_op.currentData() or "<="
        c.value = float(self._spin_value.value())
        c.consecutive = int(self._spin_consecutive.value())
        self._watcher.handler = self._cmb_handler.currentData() or ""
        self._watcher.after = self._cmb_after.currentData() or "noop"
        self._watcher.priority = int(self._spin_priority.value())
        self._watcher.cooldown_s = float(self._spin_cooldown.value())
        self._watcher.poll_min_s = float(self._spin_poll_min.value())
        self._watcher.poll_max_s = max(
            float(self._spin_poll_max.value()),
            float(self._spin_poll_min.value()),
        )
        self._watcher.enabled = self._chk_enabled.isChecked()
        self._watcher.alert_desktop = self._chk_alert.isChecked()

    def _fmt_result(self, r: EvalResult) -> str:
        c = self._watcher.condition
        if c.type in ("image_appear", "image_gone"):
            score = f"{r.score:.3f}" if r.score is not None else "—"
            cmp_ = "≥" if c.type == "image_appear" else "<"
            n = f" ({r.note})" if r.note else ""
            return f"score={score} {cmp_} threshold={c.threshold:.2f}{n}"
        if c.type == "ocr_number":
            val = f"{r.value:.0f}" if r.value is not None else "—"
            raw = f" raw={r.raw!r}" if r.raw else ""
            n = f"  [{r.note}]" if r.note else ""
            return f"値={val} {c.op} {c.value:.0f}{raw}{n}"
        return r.note or "—"

    def _test_once(self) -> None:
        self._sync_from_ui()
        c = self._watcher.condition
        if c.type in ("image_appear", "image_gone") and not c.template:
            self._append_log("⚠ テンプレ未設定（ドラッグで領域選択）")
            return
        if c.type == "ocr_number" and not c.region:
            self._append_log("⚠ 領域未設定（ドラッグで領域選択）")
            return
        if not self._window_title:
            self._append_log("⚠ 対象ウィンドウ未設定")
            return
        hwnd = find_hwnd_by_title(self._window_title)
        if not hwnd:
            self._append_log(f"⚠ ウィンドウが見つかりません: {self._window_title}")
            return
        img = capture_window(hwnd)
        if img is None:
            self._append_log("⚠ キャプチャ失敗")
            return
        r = evaluate_watcher(img, self._watcher)
        status = "✓ 発火" if r.fired else "－ 不発"
        self._append_log(f"判定: {status}  {self._fmt_result(r)}")
        # OCR テスト時は実際に OCR にかけた crop を保存して、ユーザーが
        # 領域や OCR 結果を目視できるようにする
        if c.type == "ocr_number" and c.region:
            try:
                ih, iw = img.shape[:2]
                rx, ry, rw, rh = c.region
                x0 = max(0, int(rx * iw))
                y0 = max(0, int(ry * ih))
                x1 = min(iw, int((rx + rw) * iw))
                y1 = min(ih, int((ry + rh) * ih))
                crop = img[y0:y1, x0:x1]
                if crop.size > 0:
                    os.makedirs("debug", exist_ok=True)
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    path = os.path.join("debug", f"ocr_{self._watcher.id}_{ts}.png").replace("\\", "/")
                    cv2.imwrite(path, crop)
                    self._append_log(f"  ↳ OCR入力画像: {path}")
            except Exception as e:
                self._append_log(f"  ↳ crop 保存失敗: {e}")

    # ---------------------------------------------------------------- 連続監視
    def _on_watch_state(self, watching: bool) -> None:
        if watching:
            self._btn_watch.setText("■ 監視停止")
            self._btn_watch.setStyleSheet(
                "QPushButton{background:#c62828;color:white;font-weight:bold;}"
            )
            self._btn_once.setEnabled(False)
        else:
            self._btn_watch.setText("▶ 監視開始")
            self._btn_watch.setStyleSheet("")
            self._btn_once.setEnabled(True)

    def _toggle_watch(self) -> None:
        if self._watch_thread and self._watch_thread.is_alive():
            self._stop_flag = True
            self._append_log("停止要求")
            return
        self._sync_from_ui()
        c = self._watcher.condition
        if c.type in ("image_appear", "image_gone") and not c.template:
            self._append_log("⚠ テンプレ未設定（ドラッグで領域選択）")
            return
        if c.type == "ocr_number" and not c.region:
            self._append_log("⚠ 領域未設定（ドラッグで領域選択）")
            return
        title = self._window_title
        if not title:
            self._append_log("⚠ 対象ウィンドウ未設定")
            return
        self._stop_flag = False
        self._append_log(
            f"--- 監視開始 type={c.type} consecutive={c.consecutive} "
            f"poll {self._watcher.poll_min_s:.1f}〜{self._watcher.poll_max_s:.1f}s ---"
        )

        # 監視中はスレッド側で参照する設定のスナップショットを取る
        watcher_snapshot = PcWatcher(
            id=self._watcher.id,
            title=self._watcher.title,
            enabled=True,
            priority=self._watcher.priority,
            condition=WatcherCondition(
                type=c.type,
                template=c.template,
                region=list(c.region) if c.region else None,
                threshold=c.threshold,
                ocr_whitelist=c.ocr_whitelist,
                op=c.op,
                value=c.value,
                consecutive=c.consecutive,
            ),
            cooldown_s=self._watcher.cooldown_s,
            alert_desktop=self._watcher.alert_desktop,
            poll_min_s=self._watcher.poll_min_s,
            poll_max_s=self._watcher.poll_max_s,
        )

        def _worker() -> None:
            self._watch_state_signal.emit(True)
            hit_count = 0
            last_fired = 0.0
            try:
                while not self._stop_flag:
                    hwnd = find_hwnd_by_title(title)
                    if not hwnd:
                        self._log_signal.emit(f"⚠ ウィンドウ消失: {title}")
                        break
                    img = capture_window(hwnd)
                    if img is None:
                        self._log_signal.emit("⚠ キャプチャ失敗")
                    else:
                        r = evaluate_watcher(img, watcher_snapshot)
                        info = self._fmt_result(r)
                        now = time.monotonic()
                        if r.fired:
                            hit_count += 1
                            need = max(1, watcher_snapshot.condition.consecutive)
                            if hit_count >= need:
                                if (now - last_fired) >= watcher_snapshot.cooldown_s:
                                    ts = datetime.now().strftime("%H:%M:%S")
                                    self._log_signal.emit(
                                        f"[{ts}] 🔥 発火 ({hit_count}/{need})  {info}"
                                    )
                                    last_fired = now
                                    if watcher_snapshot.alert_desktop:
                                        self._notify(watcher_snapshot)
                                else:
                                    self._log_signal.emit(
                                        f"  クール中 ({hit_count}/{need})  {info}"
                                    )
                                hit_count = 0
                            else:
                                self._log_signal.emit(
                                    f"  ヒット {hit_count}/{need}  {info}"
                                )
                        else:
                            hit_count = 0
                            self._log_signal.emit(f"  ・  {info}")
                    pmin = max(0.1, watcher_snapshot.poll_min_s)
                    pmax = max(pmin, watcher_snapshot.poll_max_s)
                    interval = random.uniform(pmin, pmax)
                    waited = 0.0
                    while waited < interval and not self._stop_flag:
                        time.sleep(min(0.1, interval - waited))
                        waited += 0.1
            except Exception as e:
                self._log_signal.emit(f"⚠ 例外: {e}")
            finally:
                self._watch_state_signal.emit(False)
                self._log_signal.emit("--- 監視終了 ---")

        self._watch_thread = threading.Thread(target=_worker, daemon=True)
        self._watch_thread.start()

    @staticmethod
    def _notify(w: PcWatcher) -> None:
        """OS のバルーン通知を試みる（pywin32 経由）。失敗しても無視。"""
        try:
            import ctypes
            ctypes.windll.user32.MessageBeep(0)  # シンプルにビープ音だけ
        except Exception:
            pass

    # ---------------------------------------------------------------- ログ
    def _append_log(self, msg: str) -> None:
        self._log.append(msg)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())
        # 発火イベント（🔥 を含む行）のみファイルにも残す
        if "🔥" in msg:
            title = self._watcher.title or self._watcher.id
            write_log(f"👁 watcher テスト発火: [{title}] {msg}")

    # ---------------------------------------------------------------- 保存
    def _save(self) -> None:
        self._sync_from_ui()
        if not self._watcher.title:
            QMessageBox.warning(self, "エラー", "タイトルを入力してください")
            return
        try:
            self._path = save_pc_watcher(self._watcher, self._path)
        except Exception as e:
            QMessageBox.warning(self, "保存失敗", str(e))
            return
        self.saved.emit(self._path)
        QMessageBox.information(self, "保存", f"保存しました: {self._path}")

    # ---------------------------------------------------------------- 終了
    def closeEvent(self, e) -> None:  # noqa: N802
        self._stop_flag = True
        self.closed.emit(self)
        super().closeEvent(e)
