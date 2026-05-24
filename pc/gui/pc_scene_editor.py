"""PC シーン編集ウィンドウ（独立して開く広めの画面）。

ワークフロー:
    1. ウィンドウから「スクショ取得」→ snapshots/ に PNG を保存し snapshot ステップを追加
    2. キャンバスをクリック → その位置を tap ステップとして追加
    3. キャンバス上をドラッグ → 領域を選択し、メニューから次を選ぶ:
       - wait_image: 領域を切り出して templates/ に保存し snapshot ステップ追加
       - tap_image:  領域を切り出して templates/ に保存し tap_image ステップ追加
       - swipe:      ドラッグの左上 → 右下を swipe ステップとして追加
    4. wait_fixed ボタンで待機秒数を追加
    5. 保存ボタンで scenes/<name>.json に書き出し

座標は全てウィンドウクライアント領域の相対比率 (0.0〜1.0)。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import Callable

import cv2
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .capture import capture_window
from .pc_canvas import PcSnapshotCanvas, RegionMarker, TapMarker
from .pc_scene import (
    SCENES_DIR,
    PcScene,
    PcStep,
    load_pc_scene,
    run_pc_scene,
    save_pc_scene,
)
from .widgets import ReorderableListWidget
from .window_picker import find_hwnd_by_title

SNAPSHOTS_DIR = "snapshots"
TEMPLATES_DIR = "templates"


class SceneEditorWindow(QWidget):
    """シーン編集ウィンドウ（独立、約 1000x800）。

    実行系はバックグラウンドスレッドで動かし、ログ・状態は Qt シグナル経由で
    メインスレッドへ反映する。
    """

    _log_signal        = Signal(str)
    _play_state_signal = Signal(bool)   # True=実行中, False=停止
    saved  = Signal(str)                # 保存完了 (パス) — メインの一覧更新トリガ
    closed = Signal(object)             # ウィンドウクローズ通知 — 参照解除用

    def __init__(
        self,
        scene_path: str | None,
        window_title: str = "",
        mouse_provider: Callable[[], object] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        # 親を None にして独立ウィンドウとして開く
        super().__init__(None)
        self.setWindowTitle("シーン編集")
        self.resize(1000, 800)

        if scene_path and os.path.exists(scene_path):
            self._scene = load_pc_scene(scene_path)
            self._path: str | None = scene_path
        else:
            stem = os.path.splitext(os.path.basename(scene_path or "新規シーン"))[0]
            self._scene = PcScene(name=stem, window_title=window_title)
            self._path = scene_path

        if not self._scene.window_title and window_title:
            self._scene.window_title = window_title

        self._current_snapshot_path: str | None = None

        # 位置再選択モード（対象行を保持。None=通常モード）
        self._edit_pos_row: int | None = None

        # call_scene 等から開いた子シーン編集ウィンドウ（自分が閉じる時にまとめて閉じる）
        self._child_scene_editors: list["SceneEditorWindow"] = []

        # 実行スレッド管理
        self._mouse_provider = mouse_provider or (lambda: None)
        self._play_thread: threading.Thread | None = None
        self._stop_flag = False

        self._log_signal.connect(self._append_run_log)
        self._play_state_signal.connect(self._on_play_state)

        self._build_ui()
        self._refresh_steps()

        # Esc: 位置編集モードの取消（子ウィジェットにフォーカスがあっても効くよう
        # QShortcut で取る。keyPressEvent は子側にフォーカスが奪われると呼ばれない）
        esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        esc.activated.connect(self._on_esc_shortcut)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # 位置再選択モードのバナー（通常は非表示）
        self._edit_banner = QLabel("")
        self._edit_banner.setStyleSheet(
            "background:#fff59d; color:#5d4037; padding:6px 10px; "
            "border:1px solid #fbc02d; border-radius:4px; font-weight:bold;"
        )
        self._edit_banner.setVisible(False)
        outer.addWidget(self._edit_banner)

        # ヘッダー
        head = QHBoxLayout()
        head.addWidget(QLabel("シーン名:"))
        self._inp_name = QLineEdit(self._scene.name)
        self._inp_name.editingFinished.connect(self._on_name_changed)
        head.addWidget(self._inp_name, 1)
        head.addSpacing(12)
        head.addWidget(QLabel("対象ウィンドウ:"))
        self._lbl_win = QLabel(self._scene.window_title or "(未設定)")
        head.addWidget(self._lbl_win)
        head.addStretch(1)
        # フロー候補チェック：チェックなら「親シーン」、外すと他から呼ばれる「部品シーン」
        self._chk_flow_target = QCheckBox("フロー候補")
        self._chk_flow_target.setChecked(self._scene.flow_target)
        self._chk_flow_target.setToolTip(
            "フロー編集の対象シーン選択肢に出すかどうか。\n"
            "チェック=エントリ用の親シーン / チェック外す=他シーンから呼ばれる部品シーン"
        )
        self._chk_flow_target.toggled.connect(self._on_flow_target_changed)
        head.addWidget(self._chk_flow_target)
        btn_save = QPushButton("保存")
        btn_save.setFixedWidth(80)
        btn_save.clicked.connect(self._save)
        head.addWidget(btn_save)
        outer.addLayout(head)

        # 中央: キャンバス + 右ペイン（ステップ一覧 + 操作）
        split = QSplitter(Qt.Horizontal)

        self._canvas = PcSnapshotCanvas()
        self._canvas.clicked.connect(self._on_canvas_clicked)
        self._canvas.region_selected.connect(self._on_canvas_region)
        split.addWidget(self._canvas)

        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(4, 0, 0, 0)

        rlay.addWidget(QLabel("操作:"))
        btn_capture = QPushButton("スクショ取得")
        btn_capture.setToolTip("対象ウィンドウからスクショを撮って snapshot ステップを追加")
        btn_capture.clicked.connect(self._capture_snapshot)
        rlay.addWidget(btn_capture)
        btn_wait = QPushButton("待機 追加")
        btn_wait.clicked.connect(self._add_wait_fixed)
        rlay.addWidget(btn_wait)
        btn_more = QPushButton("+ その他のステップ ▾")
        btn_more.clicked.connect(self._show_add_step_menu)
        rlay.addWidget(btn_more)

        # タップ後に自動で wait_fixed を追加するオプション
        tap_wait_row = QHBoxLayout()
        self._chk_tap_wait = QCheckBox("タップ後に待機")
        self._chk_tap_wait.setChecked(True)
        self._spin_tap_wait = QDoubleSpinBox()
        self._spin_tap_wait.setRange(0.1, 60.0)
        self._spin_tap_wait.setSingleStep(0.1)
        self._spin_tap_wait.setDecimals(1)
        self._spin_tap_wait.setValue(1.5)
        self._spin_tap_wait.setSuffix(" 秒")
        self._spin_tap_wait.setFixedWidth(80)
        tap_wait_row.addWidget(self._chk_tap_wait)
        tap_wait_row.addWidget(self._spin_tap_wait)
        tap_wait_row.addStretch(1)
        rlay.addLayout(tap_wait_row)
        rlay.addSpacing(8)

        rlay.addWidget(QLabel("ステップ一覧:"))
        self._list_steps = ReorderableListWidget()
        # ドラッグで並べ替え（行内のリオーダリング専用）
        self._list_steps.setDragDropMode(QAbstractItemView.InternalMove)
        self._list_steps.setDefaultDropAction(Qt.MoveAction)
        self._list_steps.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list_steps.itemSelectionChanged.connect(self._on_step_selected)
        self._list_steps.itemDoubleClicked.connect(self._on_step_double_clicked)
        self._list_steps.rows_reordered.connect(self._on_rows_reordered)
        # DEL キーで選択行を削除（このリストにフォーカスがある時だけ反応）
        del_sc = QShortcut(QKeySequence(Qt.Key_Delete), self._list_steps)
        del_sc.setContext(Qt.WidgetShortcut)
        del_sc.activated.connect(self._remove_step)
        rlay.addWidget(self._list_steps, 1)

        btn_row = QHBoxLayout()
        btn_up = QPushButton("↑")
        btn_up.clicked.connect(lambda: self._move_step(-1))
        btn_dn = QPushButton("↓")
        btn_dn.clicked.connect(lambda: self._move_step(+1))
        btn_edit = QPushButton("✏ 位置編集")
        btn_edit.setToolTip(
            "選択中ステップの位置（タップ座標 / 領域 / スワイプ両端）を"
            "キャンバス上で再選択する"
        )
        btn_edit.clicked.connect(self._enter_edit_pos_mode)
        btn_params = QPushButton("⚙ 値編集")
        btn_params.setToolTip(
            "選択中ステップの数値・テキスト系パラメータを編集"
            "（待ち秒数・閾値・タイムアウト・キー名 など）"
        )
        btn_params.clicked.connect(self._edit_step_params)
        btn_open_ref = QPushButton("📂 呼出し先")
        btn_open_ref.setToolTip(
            "選択中ステップが参照しているシーンを別ウィンドウで開く"
            "（call_scene / pick_scene / if_image 対応）"
        )
        btn_open_ref.clicked.connect(self._open_referenced_scene)
        btn_del = QPushButton("削除")
        btn_del.clicked.connect(self._remove_step)
        btn_row.addWidget(btn_up)
        btn_row.addWidget(btn_dn)
        btn_row.addWidget(btn_edit)
        btn_row.addWidget(btn_params)
        btn_row.addWidget(btn_open_ref)
        btn_row.addWidget(btn_del)
        rlay.addLayout(btn_row)

        # 実行コントロール
        run_row = QHBoxLayout()
        self._btn_run_one = QPushButton("選択行を実行")
        self._btn_run_one.clicked.connect(self._run_selected_step)
        self._btn_play    = QPushButton("▶ 再生")
        self._btn_play.clicked.connect(self._toggle_play)
        run_row.addWidget(self._btn_run_one)
        run_row.addWidget(self._btn_play)
        rlay.addLayout(run_row)

        rlay.addWidget(QLabel("実行ログ:"))
        self._run_log = QTextEdit()
        self._run_log.setReadOnly(True)
        self._run_log.setMaximumHeight(180)
        self._run_log.setStyleSheet(
            "font-family: Consolas, monospace; font-size:11px;"
        )
        rlay.addWidget(self._run_log)

        hint = QLabel(
            "操作: クリック=タップ追加 / ドラッグ=領域選択→メニュー / "
            "右クリック保持+ホイール=拡大縮小・右ドラッグ=パン"
        )
        hint.setStyleSheet("color:#666; font-size:11px;")
        hint.setWordWrap(True)
        rlay.addWidget(hint)

        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([700, 300])
        outer.addWidget(split, 1)

    # ---------------------------------------------------------------- スクショ
    def _capture_snapshot(self) -> None:
        if not self._scene.window_title:
            QMessageBox.warning(self, "エラー", "対象ウィンドウが未設定です（メイン画面で選択）")
            return
        hwnd = find_hwnd_by_title(self._scene.window_title)
        if not hwnd:
            QMessageBox.warning(
                self, "エラー",
                f"ウィンドウが見つかりません: {self._scene.window_title}",
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
        self._scene.steps.append(PcStep(
            type="snapshot",
            params={"path": path, "threshold": 0.85, "timeout_s": 10.0},
        ))
        self._current_snapshot_path = path
        self._canvas.set_snapshot(path)
        self._refresh_steps()
        self._list_steps.setCurrentRow(len(self._scene.steps) - 1)

    # ------------------------------------------------------- クリック → tap
    def _on_canvas_clicked(self, rx: float, ry: float) -> None:
        if self._edit_pos_row is not None:
            self._apply_edit_pos_click(rx, ry)
            return
        self._scene.steps.append(PcStep(
            type="tap",
            params={"rx": round(rx, 4), "ry": round(ry, 4), "duration_ms": 50},
        ))
        # 「タップ後に待機」が ON ならセットで wait_fixed も追加
        if self._chk_tap_wait.isChecked():
            self._scene.steps.append(PcStep(
                type="wait_fixed",
                params={"seconds": float(self._spin_tap_wait.value())},
            ))
        self._refresh_steps()
        self._list_steps.setCurrentRow(len(self._scene.steps) - 1)

    # ------------------------------------------------- ドラッグ → 領域メニュー
    def _on_canvas_region(self, rx: float, ry: float, rw: float, rh: float) -> None:
        if self._edit_pos_row is not None:
            self._apply_edit_pos_region(rx, ry, rw, rh)
            return
        if self._current_snapshot_path is None:
            QMessageBox.warning(self, "エラー", "先にスクショを取得してください")
            return

        menu = QMenu(self)
        act_wait   = menu.addAction("画像出現待ち  (この画像が出るまで待つ)")
        act_tap    = menu.addAction("画像をタップ  (この画像を見つけてタップ)")
        menu.addSeparator()
        act_swipe  = menu.addAction("スワイプ      (左上 → 右下)")
        act_scroll = menu.addAction("スクロール    (ジッター付きスワイプ)")
        menu.addSeparator()
        act_if     = menu.addAction("画像で分岐    (画像有無で then / else)")
        action = menu.exec(QCursor.pos())
        if action is None:
            return

        if action is act_swipe or action is act_scroll:
            t = "swipe" if action is act_swipe else "scroll"
            params: dict = {
                "rx1": round(rx, 4), "ry1": round(ry, 4),
                "rx2": round(rx + rw, 4), "ry2": round(ry + rh, 4),
                "duration_ms": 500,
            }
            if t == "scroll":
                # ジッターのデフォルトを領域比率と連動して 1% 程度に
                params["rx1_jitter"] = 0.01
                params["ry1_jitter"] = 0.01
                params["rx2_jitter"] = 0.01
                params["ry2_jitter"] = 0.01
                params["duration_jitter_ms"] = 100
            self._scene.steps.append(PcStep(type=t, params=params))
            self._refresh_steps()
            self._list_steps.setCurrentRow(len(self._scene.steps) - 1)
            return

        if action is act_if:
            self._add_if_image(rx, ry, rw, rh)
            return

        # wait_image / tap_image: テンプレートを切り出して保存
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
            QMessageBox.warning(self, "エラー", "領域サイズが小さすぎます")
            return

        scene_dir = os.path.join(TEMPLATES_DIR, self._scene.name or "scene")
        os.makedirs(scene_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        tpl_path = os.path.join(scene_dir, f"tpl_{ts}.png").replace("\\", "/")
        cv2.imwrite(tpl_path, crop)

        if action is act_wait:
            self._scene.steps.append(PcStep(
                type="snapshot",
                params={"path": tpl_path, "threshold": 0.85, "timeout_s": 10.0},
            ))
        else:  # tap_image
            self._scene.steps.append(PcStep(
                type="tap_image",
                params={
                    "template": tpl_path,
                    "threshold": 0.85,
                    "timeout_s": 10.0,
                    "region": [round(rx, 4), round(ry, 4), round(rw, 4), round(rh, 4)],
                    "duration_ms": 50,
                },
            ))
        self._refresh_steps()
        self._list_steps.setCurrentRow(len(self._scene.steps) - 1)

    # --------------------------------------------------- 位置再選択モード
    # 対応タイプと、クリック / ドラッグのどちらを受けるか
    _EDIT_POS_CLICK_TYPES  = {"tap"}
    _EDIT_POS_REGION_TYPES = {"tap_image", "snapshot", "swipe", "scroll", "if_image"}

    def _enter_edit_pos_mode(self) -> None:
        row = self._list_steps.currentRow()
        if row < 0 or row >= len(self._scene.steps):
            self._append_run_log("⚠ ステップ未選択")
            return
        step = self._scene.steps[row]
        if (
            step.type not in self._EDIT_POS_CLICK_TYPES
            and step.type not in self._EDIT_POS_REGION_TYPES
        ):
            self._append_run_log(
                f"⚠ {step.type} は位置を持たないため位置編集できません"
            )
            return
        # snapshot のうち、フル画面キャプチャ（snapshots/ 配下）は対象外
        if step.type == "snapshot":
            path = str(step.params.get("path", ""))
            if not path.startswith(TEMPLATES_DIR):
                self._append_run_log(
                    "⚠ この snapshot はフル画面キャプチャ。位置編集はテンプレ画像のみ対応"
                )
                return
        if self._current_snapshot_path is None:
            QMessageBox.warning(
                self, "エラー",
                "対応する直前スクショが表示されていません",
            )
            return
        self._edit_pos_row = row
        self._update_edit_pos_banner()
        self._canvas.setFocus()

    def _cancel_edit_pos_mode(self) -> None:
        if self._edit_pos_row is None:
            return
        self._edit_pos_row = None
        self._update_edit_pos_banner()
        self._append_run_log("位置編集を取消")

    def _update_edit_pos_banner(self) -> None:
        if self._edit_pos_row is None:
            self._edit_banner.setVisible(False)
            self._canvas.unsetCursor()
            return
        step = self._scene.steps[self._edit_pos_row]
        hints = {
            "tap":       "クリックで位置を更新",
            "tap_image": "ドラッグで領域を更新（テンプレ画像を再切出し）",
            "snapshot":  "ドラッグで領域を更新（テンプレ画像を再切出し）",
            "swipe":     "ドラッグで開始 → 終了を更新",
            "scroll":    "ドラッグで開始 → 終了を更新",
            "if_image":  "ドラッグで領域を更新（テンプレ画像を再切出し）",
        }
        self._edit_banner.setText(
            f"📍 #{self._edit_pos_row + 1} {step.type} の位置を再選択中…  "
            f"{hints.get(step.type, '')}　[Esc で取消]"
        )
        self._edit_banner.setVisible(True)
        self._canvas.setCursor(Qt.CrossCursor)

    def _apply_edit_pos_click(self, rx: float, ry: float) -> None:
        row = self._edit_pos_row
        if row is None or row >= len(self._scene.steps):
            self._cancel_edit_pos_mode()
            return
        step = self._scene.steps[row]
        if step.type != "tap":
            self._append_run_log(
                f"⚠ {step.type} はドラッグで領域を選択してください（クリックは tap のみ）"
            )
            return
        step.params["rx"] = round(rx, 4)
        step.params["ry"] = round(ry, 4)
        self._append_run_log(f"✓ #{row + 1} tap 位置を更新")
        self._finish_edit_pos(row)

    def _apply_edit_pos_region(
        self, rx: float, ry: float, rw: float, rh: float,
    ) -> None:
        row = self._edit_pos_row
        if row is None or row >= len(self._scene.steps):
            self._cancel_edit_pos_mode()
            return
        step = self._scene.steps[row]
        if step.type == "tap":
            self._append_run_log("⚠ tap はクリック（ドラッグ不可）で位置を指定してください")
            return
        if step.type in ("swipe", "scroll"):
            step.params["rx1"] = round(rx, 4)
            step.params["ry1"] = round(ry, 4)
            step.params["rx2"] = round(rx + rw, 4)
            step.params["ry2"] = round(ry + rh, 4)
            self._append_run_log(f"✓ #{row + 1} {step.type} 始終点を更新")
            self._finish_edit_pos(row)
            return
        # tap_image / snapshot / if_image: テンプレを切り出し直す
        if self._current_snapshot_path is None:
            QMessageBox.warning(self, "エラー", "スナップ画像がありません")
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
            QMessageBox.warning(self, "エラー", "領域サイズが小さすぎます")
            return
        scene_dir = os.path.join(TEMPLATES_DIR, self._scene.name or "scene")
        os.makedirs(scene_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        tpl_path = os.path.join(scene_dir, f"tpl_{ts}.png").replace("\\", "/")
        cv2.imwrite(tpl_path, crop)
        if step.type == "snapshot":
            step.params["path"] = tpl_path
        else:  # tap_image / if_image
            step.params["template"] = tpl_path
        step.params["region"] = [
            round(rx, 4), round(ry, 4), round(rw, 4), round(rh, 4),
        ]
        self._append_run_log(
            f"✓ #{row + 1} {step.type} 領域＋テンプレを更新 ({os.path.basename(tpl_path)})"
        )
        self._finish_edit_pos(row)

    def _finish_edit_pos(self, row: int) -> None:
        self._edit_pos_row = None
        self._update_edit_pos_banner()
        self._refresh_steps()
        self._list_steps.setCurrentRow(row)

    def _on_esc_shortcut(self) -> None:
        if self._edit_pos_row is not None:
            self._cancel_edit_pos_mode()

    # --------------------------------------------------- ステップ値編集
    _SCENE_REF_TYPES = {"call_scene", "pick_scene", "if_image"}

    def _on_step_double_clicked(self, *_args) -> None:
        """ダブルクリック振り分け:
        - 位置を持つステップ → 位置編集モード
        - シーン参照ステップ → 呼出し先を開く
        - それ以外 → 値編集ダイアログ
        """
        row = self._list_steps.currentRow()
        if row < 0 or row >= len(self._scene.steps):
            return
        step = self._scene.steps[row]
        pos_types = self._EDIT_POS_CLICK_TYPES | self._EDIT_POS_REGION_TYPES
        if step.type in pos_types:
            self._enter_edit_pos_mode()
        elif step.type in self._SCENE_REF_TYPES:
            self._open_referenced_scene()
        else:
            self._edit_step_params()

    def _open_referenced_scene(self) -> None:
        """選択中ステップが参照するシーンを子編集ウィンドウで開く。"""
        row = self._list_steps.currentRow()
        if row < 0 or row >= len(self._scene.steps):
            self._append_run_log("⚠ ステップ未選択")
            return
        step = self._scene.steps[row]
        candidates: list[str] = []
        if step.type == "call_scene":
            s = str(step.params.get("scene", "")).strip()
            if s:
                candidates.append(s)
        elif step.type == "pick_scene":
            for s in step.params.get("scenes", []) or []:
                s = str(s).strip()
                if s:
                    candidates.append(s)
        elif step.type == "if_image":
            for key in ("then_scene", "else_scene"):
                s = str(step.params.get(key, "")).strip()
                if s:
                    candidates.append(s)
        else:
            self._append_run_log(
                f"⚠ {step.type} はシーン参照を持ちません"
            )
            return
        if not candidates:
            self._append_run_log("⚠ 参照しているシーンがありません")
            return
        if len(candidates) == 1:
            target = candidates[0]
        else:
            target, ok = QInputDialog.getItem(
                self, "呼出し先を開く",
                "編集するシーン:", candidates, 0, False,
            )
            if not ok:
                return
        self._open_child_scene(target)

    def _open_child_scene(self, fname: str) -> None:
        if not fname:
            return
        if not fname.endswith(".json"):
            fname = f"{fname}.json"
        path = os.path.join(SCENES_DIR, fname)
        if not os.path.exists(path):
            QMessageBox.warning(
                self, "エラー", f"シーンが見つかりません: {fname}",
            )
            return
        # 自分自身を再帰的に開く形になるが、別インスタンスなのでループはしない
        win = SceneEditorWindow(
            path,
            window_title=self._scene.window_title,
            mouse_provider=self._mouse_provider,
        )
        # 子の保存通知を自分の saved に流せば、最終的にメインの一覧更新まで届く
        win.saved.connect(self.saved.emit)
        win.closed.connect(self._on_child_scene_closed)
        self._child_scene_editors.append(win)
        win.show()
        self._append_run_log(f"子シーン編集を開いた: {fname}")

    def _on_child_scene_closed(self, win) -> None:
        if win in self._child_scene_editors:
            self._child_scene_editors.remove(win)

    # ステップタイプ → 値編集ハンドラ。返り値 True で変更ありとして再描画
    def _edit_step_params(self) -> None:
        row = self._list_steps.currentRow()
        if row < 0 or row >= len(self._scene.steps):
            self._append_run_log("⚠ ステップ未選択")
            return
        step = self._scene.steps[row]
        handlers = {
            "wait_fixed":   self._edit_params_wait_fixed,
            "keyevent":     self._edit_params_keyevent,
            "group_header": self._edit_params_group_header,
        }
        h = handlers.get(step.type)
        if h is None:
            self._append_run_log(
                f"⚠ {step.type} の値編集はまだ未対応"
                "（位置/領域のみ ✏ で再選択可能）"
            )
            return
        if h(step):
            self._refresh_steps()
            self._list_steps.setCurrentRow(row)

    def _edit_params_wait_fixed(self, step: PcStep) -> bool:
        cur = float(step.params.get("seconds", 1.0))
        v, ok = QInputDialog.getDouble(
            self, "待機秒数", "待機秒数:", cur, 0.1, 600.0, 1,
        )
        if not ok:
            return False
        step.params["seconds"] = v
        return True

    def _edit_params_keyevent(self, step: PcStep) -> bool:
        key = str(step.params.get("key", ""))
        new_key, ok = QInputDialog.getText(
            self, "キー入力",
            "キー名 (例: esc, enter, f5, a, space):", text=key,
        )
        if not ok:
            return False
        new_key = new_key.strip()
        if not new_key:
            return False
        dur = int(step.params.get("duration_ms", 30))
        new_dur, ok = QInputDialog.getInt(
            self, "キー入力", "押下時間 (ms):", dur, 1, 5000, 10,
        )
        if not ok:
            return False
        step.params["key"] = new_key
        step.params["duration_ms"] = new_dur
        return True

    def _edit_params_group_header(self, step: PcStep) -> bool:
        cur = str(step.params.get("label", ""))
        text, ok = QInputDialog.getText(
            self, "見出し", "見出し文字列:", text=cur,
        )
        if not ok:
            return False
        step.params["label"] = text.strip()
        return True

    def _add_wait_fixed(self) -> None:
        v, ok = QInputDialog.getDouble(
            self, "待機", "待機秒数:", 1.0, 0.1, 600.0, 1,
        )
        if not ok:
            return
        self._scene.steps.append(PcStep(type="wait_fixed", params={"seconds": v}))
        self._refresh_steps()
        self._list_steps.setCurrentRow(len(self._scene.steps) - 1)

    # ---- B 系ステップの追加ヘルパー
    def _list_scenes(self) -> list[str]:
        if not os.path.isdir(SCENES_DIR):
            return []
        return [f for f in sorted(os.listdir(SCENES_DIR)) if f.endswith(".json")]

    def _show_add_step_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("シーン呼び出し 追加", self._add_call_scene)
        menu.addAction("シーン抽選 追加",     self._add_pick_scene)
        menu.addAction("キー入力 追加",       self._add_keyevent)
        menu.addAction("見出し 追加",         self._add_group_header)
        menu.exec(QCursor.pos())

    def _add_call_scene(self) -> None:
        scenes = self._list_scenes()
        if not scenes:
            QMessageBox.information(self, "シーン一覧", "scenes/ にシーンがありません")
            return
        item, ok = QInputDialog.getItem(
            self, "シーン呼び出し", "呼び出すシーン:", scenes, 0, False,
        )
        if not ok:
            return
        self._scene.steps.append(PcStep(type="call_scene", params={"scene": item}))
        self._refresh_steps()
        self._list_steps.setCurrentRow(len(self._scene.steps) - 1)

    def _add_pick_scene(self) -> None:
        scenes = self._list_scenes()
        if not scenes:
            QMessageBox.information(self, "シーン一覧", "scenes/ にシーンがありません")
            return
        text, ok = QInputDialog.getMultiLineText(
            self, "シーン抽選",
            "1 行 1 シーン名（既存シーン名で空行は無視）:",
            "\n".join(scenes[:2]),
        )
        if not ok:
            return
        chosen = [s.strip() for s in text.splitlines() if s.strip()]
        if not chosen:
            return
        mode_choices = {"ランダム": "random", "順番": "sequential"}
        label, ok = QInputDialog.getItem(
            self, "シーン抽選", "選択モード:",
            list(mode_choices.keys()), 0, False,
        )
        if not ok:
            return
        mode = mode_choices[label]
        self._scene.steps.append(PcStep(
            type="pick_scene",
            params={"mode": mode, "scenes": chosen},
        ))
        self._refresh_steps()
        self._list_steps.setCurrentRow(len(self._scene.steps) - 1)

    def _add_keyevent(self) -> None:
        key, ok = QInputDialog.getText(
            self, "キー入力",
            "キー名 (例: esc, enter, f5, a, space):",
        )
        if not ok or not key.strip():
            return
        self._scene.steps.append(PcStep(
            type="keyevent",
            params={"key": key.strip(), "duration_ms": 30},
        ))
        self._refresh_steps()
        self._list_steps.setCurrentRow(len(self._scene.steps) - 1)

    def _add_group_header(self) -> None:
        label, ok = QInputDialog.getText(
            self, "見出し", "見出し文字列:",
        )
        if not ok:
            return
        self._scene.steps.append(PcStep(
            type="group_header",
            params={"label": label.strip()},
        ))
        self._refresh_steps()
        self._list_steps.setCurrentRow(len(self._scene.steps) - 1)

    def _add_if_image(self, rx: float, ry: float, rw: float, rh: float) -> None:
        if self._current_snapshot_path is None:
            QMessageBox.warning(self, "エラー", "先にスクショを取得してください")
            return
        # 領域を切り出してテンプレに保存
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
            QMessageBox.warning(self, "エラー", "領域サイズが小さすぎます")
            return
        scene_dir = os.path.join(TEMPLATES_DIR, self._scene.name or "scene")
        os.makedirs(scene_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        tpl_path = os.path.join(scene_dir, f"tpl_{ts}.png").replace("\\", "/")
        cv2.imwrite(tpl_path, crop)

        scenes = self._list_scenes()
        if not scenes:
            QMessageBox.information(self, "画像で分岐", "scenes/ にシーンが無いため空で追加します")
            then_name = ""
            else_name = ""
        else:
            then_name, ok = QInputDialog.getItem(
                self, "画像で分岐", "成立（画像あり）→ 実行シーン:",
                ["(なし)"] + scenes, 0, False,
            )
            if not ok:
                then_name = ""
            elif then_name == "(なし)":
                then_name = ""
            else_name, ok = QInputDialog.getItem(
                self, "画像で分岐", "不成立（画像なし）→ 実行シーン:",
                ["(なし)"] + scenes, 0, False,
            )
            if not ok:
                else_name = ""
            elif else_name == "(なし)":
                else_name = ""

        self._scene.steps.append(PcStep(
            type="if_image",
            params={
                "template": tpl_path,
                "threshold": 0.85,
                "region": [round(rx, 4), round(ry, 4), round(rw, 4), round(rh, 4)],
                "then_scene": then_name,
                "else_scene": else_name,
            },
        ))
        self._refresh_steps()
        self._list_steps.setCurrentRow(len(self._scene.steps) - 1)

    # ---------------------------------------------------------------- ステップ一覧
    def _refresh_steps(self) -> None:
        cur = self._list_steps.currentRow()
        # ドラッグ並べ替えの rows_reordered と循環しないようシグナルを止める
        self._list_steps.blockSignals(True)
        try:
            self._list_steps.clear()
            for i, s in enumerate(self._scene.steps):
                item = QListWidgetItem(self._step_label(i, s))
                # 並べ替え後の照合用にステップオブジェクトの id を持たせる
                item.setData(Qt.UserRole, id(s))
                self._list_steps.addItem(item)
        finally:
            self._list_steps.blockSignals(False)
        if 0 <= cur < len(self._scene.steps):
            self._list_steps.setCurrentRow(cur)
        self._refresh_markers()

    def _on_rows_reordered(self) -> None:
        """ドラッグで行が動いた後、self._scene.steps を新しい順に組み直す。"""
        id_to_step = {id(s): s for s in self._scene.steps}
        new_steps: list[PcStep] = []
        for i in range(self._list_steps.count()):
            sid = self._list_steps.item(i).data(Qt.UserRole)
            s = id_to_step.get(sid)
            if s is not None:
                new_steps.append(s)
        if len(new_steps) != len(self._scene.steps):
            # 想定外（紐づけが取れない行が混ざった）: 安全側に倒して描き直しのみ
            self._refresh_steps()
            return
        # 移動前に選択していたステップを、移動後の行で再選択
        cur_row = self._list_steps.currentRow()
        cur_id = (
            self._list_steps.item(cur_row).data(Qt.UserRole)
            if 0 <= cur_row < self._list_steps.count()
            else None
        )
        self._scene.steps = new_steps
        self._refresh_steps()
        if cur_id is not None:
            for i, s in enumerate(self._scene.steps):
                if id(s) == cur_id:
                    self._list_steps.setCurrentRow(i)
                    break

    def _step_label(self, i: int, s: PcStep) -> str:
        p = s.params
        if s.type == "wait_fixed":
            return f"{i+1:02d}. ⏳ 待機  {p.get('seconds', 0)} 秒"
        if s.type == "snapshot":
            name = os.path.basename(str(p.get("path", "")))
            return (
                f"{i+1:02d}. 📸 画像出現待ち  {name}  "
                f"閾値={p.get('threshold', 0.85)}"
            )
        if s.type == "tap":
            return (
                f"{i+1:02d}. 👆 タップ  "
                f"({p.get('rx', 0):.3f}, {p.get('ry', 0):.3f})"
            )
        if s.type == "tap_image":
            name = os.path.basename(str(p.get("template", "")))
            return f"{i+1:02d}. 🎯 画像をタップ  {name}"
        if s.type == "swipe":
            return (
                f"{i+1:02d}. ↔ スワイプ  "
                f"({p.get('rx1', 0):.3f},{p.get('ry1', 0):.3f}) → "
                f"({p.get('rx2', 0):.3f},{p.get('ry2', 0):.3f})"
            )
        if s.type == "scroll":
            return (
                f"{i+1:02d}. 🌀 スクロール（ジッター付き）  "
                f"({p.get('rx1', 0):.3f},{p.get('ry1', 0):.3f}) → "
                f"({p.get('rx2', 0):.3f},{p.get('ry2', 0):.3f})"
            )
        if s.type == "call_scene":
            return f"{i+1:02d}. 📞 シーン呼び出し → {p.get('scene', '')}"
        if s.type == "if_image":
            name = os.path.basename(str(p.get("template", "")))
            return (
                f"{i+1:02d}. ❓ 画像で分岐  {name}  "
                f"成立→{p.get('then_scene', '')}  不成立→{p.get('else_scene', '')}"
            )
        if s.type == "pick_scene":
            scenes = p.get("scenes", []) or []
            mode_jp = "ランダム" if p.get("mode", "random") == "random" else "順番"
            return f"{i+1:02d}. 🎲 シーン抽選[{mode_jp}]  ({len(scenes)} 件)"
        if s.type == "keyevent":
            return f"{i+1:02d}. ⌨ キー入力  {p.get('key', '')!r}"
        if s.type == "group_header":
            return f"━━ {p.get('label', '')} ━━"
        return f"{i+1:02d}. {s.type}"

    def _on_step_selected(self) -> None:
        # 選択中ステップの直前 snapshot を表示
        row = self._list_steps.currentRow()
        if row < 0 or row >= len(self._scene.steps):
            self._refresh_markers()
            return
        snap_path: str | None = None
        for k in range(row, -1, -1):
            sk = self._scene.steps[k]
            if sk.type == "snapshot":
                snap_path = sk.params.get("path")
                break
        if snap_path and snap_path != self._current_snapshot_path:
            if self._canvas.set_snapshot(snap_path):
                self._current_snapshot_path = snap_path
        self._refresh_markers()

    def _refresh_markers(self) -> None:
        """現スナップショット以降〜次の snapshot 直前までのマーカーを描く。"""
        taps: list[TapMarker] = []
        regions: list[RegionMarker] = []
        snap_idx = -1
        for i, s in enumerate(self._scene.steps):
            if s.type == "snapshot" and s.params.get("path") == self._current_snapshot_path:
                snap_idx = i
        if snap_idx < 0:
            self._canvas.set_markers(taps=[], regions=[])
            return
        for j in range(snap_idx + 1, len(self._scene.steps)):
            s = self._scene.steps[j]
            if s.type == "snapshot":
                break
            label = str(j + 1)
            if s.type == "tap":
                taps.append(TapMarker(
                    rx=float(s.params.get("rx", 0)),
                    ry=float(s.params.get("ry", 0)),
                    label=label,
                ))
            elif s.type == "tap_image":
                rg = s.params.get("region")
                if rg and len(rg) == 4:
                    regions.append(RegionMarker(
                        rx=float(rg[0]), ry=float(rg[1]),
                        rw=float(rg[2]), rh=float(rg[3]),
                        label=f"{label} tap_image",
                    ))
            elif s.type == "swipe":
                taps.append(TapMarker(
                    rx=float(s.params.get("rx1", 0)),
                    ry=float(s.params.get("ry1", 0)),
                    label=f"{label}A",
                ))
                taps.append(TapMarker(
                    rx=float(s.params.get("rx2", 0)),
                    ry=float(s.params.get("ry2", 0)),
                    label=f"{label}B",
                ))
        self._canvas.set_markers(taps=taps, regions=regions)

    def _remove_step(self) -> None:
        row = self._list_steps.currentRow()
        if row < 0:
            return
        del self._scene.steps[row]
        self._refresh_steps()
        if row >= len(self._scene.steps):
            row = len(self._scene.steps) - 1
        if row >= 0:
            self._list_steps.setCurrentRow(row)

    def _move_step(self, direction: int) -> None:
        row = self._list_steps.currentRow()
        if row < 0:
            return
        new_row = row + direction
        if new_row < 0 or new_row >= len(self._scene.steps):
            return
        self._scene.steps[row], self._scene.steps[new_row] = (
            self._scene.steps[new_row], self._scene.steps[row],
        )
        self._refresh_steps()
        self._list_steps.setCurrentRow(new_row)

    def _on_name_changed(self) -> None:
        name = self._inp_name.text().strip()
        if name:
            self._scene.name = name

    def _on_flow_target_changed(self, checked: bool) -> None:
        self._scene.flow_target = bool(checked)

    # ---------------------------------------------------------------- 実行
    def _append_run_log(self, msg: str) -> None:
        self._run_log.append(msg)
        sb = self._run_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_play_state(self, playing: bool) -> None:
        if playing:
            self._btn_play.setText("■ 停止")
            self._btn_play.setStyleSheet(
                "QPushButton{background:#c62828;color:white;font-weight:bold;}"
            )
            self._btn_run_one.setEnabled(False)
        else:
            self._btn_play.setText("▶ 再生")
            self._btn_play.setStyleSheet("")
            self._btn_run_one.setEnabled(True)

    def _run_selected_step(self) -> None:
        if self._play_thread and self._play_thread.is_alive():
            self._append_run_log("⚠ 実行中です")
            return
        row = self._list_steps.currentRow()
        if row < 0 or row >= len(self._scene.steps):
            self._append_run_log("⚠ ステップ未選択")
            return
        step = self._scene.steps[row]
        self._append_run_log(f"--- 単発実行 #{row + 1}: {step.type} ---")
        self._launch_thread([step])

    def _toggle_play(self) -> None:
        if self._play_thread and self._play_thread.is_alive():
            self._stop_flag = True
            self._append_run_log("停止要求")
            return
        if not self._scene.steps:
            self._append_run_log("⚠ ステップがありません")
            return
        self._append_run_log(f"--- 再生: {self._scene.name} ({len(self._scene.steps)} ステップ) ---")
        self._launch_thread(list(self._scene.steps))

    def _launch_thread(self, steps: list[PcStep]) -> None:
        self._stop_flag = False
        title = self._scene.window_title
        # 実行用に一時シーンを作る
        tmp_scene = PcScene(
            name=f"_run_{self._scene.name}",
            window_title=title,
            steps=steps,
        )
        mouse = self._mouse_provider()
        hwnd = find_hwnd_by_title(title) if title else None
        if not hwnd:
            self._append_run_log(f"⚠ ウィンドウが見つかりません: {title}")
            return
        if mouse is None:
            self._append_run_log("⚠ Pico 未接続（tap/swipe/tap_image はスキップされます）")

        def _worker() -> None:
            self._play_state_signal.emit(True)
            try:
                ok = run_pc_scene(
                    tmp_scene,
                    mouse=mouse,
                    hwnd=hwnd,
                    log=self._log_signal.emit,
                    should_stop=lambda: self._stop_flag,
                )
                self._log_signal.emit("完了" if ok else "中断/失敗")
            except Exception as e:
                self._log_signal.emit(f"⚠ 例外: {e}")
            finally:
                self._play_state_signal.emit(False)

        self._play_thread = threading.Thread(target=_worker, daemon=True)
        self._play_thread.start()

    # ---------------------------------------------------------------- 保存
    def _save(self) -> None:
        self._on_name_changed()
        if not self._scene.name:
            QMessageBox.warning(self, "エラー", "シーン名を入力してください")
            return
        if self._path is None:
            self._path = os.path.join(SCENES_DIR, f"{self._scene.name}.json")
        try:
            save_pc_scene(self._scene, self._path)
        except Exception as e:
            QMessageBox.warning(self, "保存失敗", str(e))
            return
        self.saved.emit(self._path)
        QMessageBox.information(self, "保存", f"保存しました: {self._path}")

    def closeEvent(self, e) -> None:  # noqa: N802
        self._stop_flag = True
        # 自分から開いた子シーン編集ウィンドウもまとめて閉じる
        for win in list(self._child_scene_editors):
            try:
                win.close()
            except Exception:
                pass
        self.closed.emit(self)
        super().closeEvent(e)
