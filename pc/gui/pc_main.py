"""PC フロー制御 メインウィンドウ。

縦長レイアウト（Win11 スナップで画面 1/4 程度を想定: 約 480x1080）。

レイアウト:
    ┌─ ヘッダー（常設） ─────────────────┐
    │  ゲームウィンドウ選択              │
    │  Pico マウス接続                   │
    ├─ タブ ────────────────────────────┤
    │  [実行][テスト][見張][作成][ログ] │
    ├───────────────────────────────────┤
    │  （タブ内容）                      │
    └───────────────────────────────────┘
"""
from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIntValidator, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSlider,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .exp_meter import ExpMeter
from .logger import DEFAULT_LOG_RETAIN_DAYS, purge_old_logs, write_log
from .pc_flow import (
    DAY_NAMES,
    FLOWS_DIR,
    PcFlowRunner,
    entry_scenes,
    load_pc_flow,
)
from .pc_scene import SCENES_DIR
from .pc_scene_editor import SceneEditorWindow
from .pc_watcher import WATCHERS_DIR, load_pc_watcher, save_pc_watcher
from .pc_watcher_editor import WatcherEditorWindow
from .settings import load_settings, save_settings
from .tesseract import apply_path as apply_tesseract_path, detect_tesseract
from .window_picker import WindowPickerDialog, find_hwnd_by_title

try:
    from pico_mouse import PicoMouse, find_pico_port
except ImportError:
    PicoMouse = None        # type: ignore[assignment,misc]
    find_pico_port = None   # type: ignore[assignment]


class PcFlowWindow(QWidget):
    """PC フロー制御メインウィンドウ（縦長タブ構成）。"""

    def __init__(self, settings: dict) -> None:
        super().__init__()
        self._settings = settings
        self._mouse: "PicoMouse | None" = None
        self._runner = PcFlowRunner()

        self.setWindowTitle("PC フロー制御")
        self.setMinimumWidth(360)
        self.resize(480, 900)

        self._build_ui()
        self._connect_signals()
        self._purge_old_logs()
        self._setup_tesseract()
        self._load_flows_list()
        self._restore_settings()

        QTimer.singleShot(400, self._auto_connect_pico)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_status)
        self._refresh_timer.start(1000)

        # クリック座標キャプチャ用オーバーレイ
        self._capture_overlay: "_ClickCaptureOverlay | None" = None
        self._capture_count: int = 0

    # ---------------------------------------------------------------- UI 構築
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(6)
        outer.setContentsMargins(8, 8, 8, 8)

        outer.addWidget(self._build_header_window())
        outer.addWidget(self._build_header_pico())

        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.North)
        self._tabs.addTab(self._build_tab_run(),     "実行")
        self._tabs.addTab(self._build_tab_test(),    "テスト")
        self._tabs.addTab(self._build_tab_watcher(), "見張り")
        self._tabs.addTab(self._build_tab_editor(),  "作成")
        self._tabs.addTab(self._build_tab_log(),     "ログ")
        outer.addWidget(self._tabs, 1)

        # テストタブは Pico 接続時のみ有効
        self._set_test_enabled(False)

    def _set_test_enabled(self, enabled: bool) -> None:
        """テストタブの操作ボタンを Pico 接続状態に応じて enable/disable する。"""
        for btn in (
            self._btn_test_getpos,
            self._btn_test_capture,
            self._btn_test_pico_move,
            self._btn_test_calibrate,
            self._btn_test_lclick,
            self._btn_test_rclick,
            self._btn_test_drag,
        ):
            btn.setEnabled(enabled)

    # ---- ヘッダー: ゲームウィンドウ
    def _build_header_window(self) -> QWidget:
        grp = QGroupBox("ゲームウィンドウ")
        lay = QHBoxLayout(grp)
        lay.setContentsMargins(8, 6, 8, 6)
        self._lbl_win = QLabel("未設定")
        self._lbl_win.setWordWrap(True)
        self._btn_win = QPushButton("選択…")
        self._btn_win.setFixedWidth(64)
        self._btn_win.clicked.connect(self._pick_window)
        lay.addWidget(self._lbl_win, 1)
        lay.addWidget(self._btn_win)
        return grp

    # ---- ヘッダー: Pico マウス
    def _build_header_pico(self) -> QWidget:
        grp = QGroupBox("Pico マウス")
        lay = QHBoxLayout(grp)
        lay.setContentsMargins(8, 6, 8, 6)
        self._lbl_pico = QLabel("未接続")
        self._btn_pico = QPushButton("接続")
        self._btn_pico.setFixedWidth(64)
        self._btn_pico.clicked.connect(self._connect_pico)
        lay.addWidget(self._lbl_pico, 1)
        lay.addWidget(self._btn_pico)
        return grp

    # ---- 実行タブ
    def _build_tab_run(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 8, 6, 6)
        lay.setSpacing(6)

        flow_row = QHBoxLayout()
        self._combo_flow = QComboBox()
        self._combo_flow.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._combo_flow.currentIndexChanged.connect(self._on_flow_selected)
        self._btn_flow = QPushButton("開始")
        self._btn_flow.setFixedWidth(60)
        self._btn_flow.clicked.connect(self._toggle_flow)
        flow_row.addWidget(QLabel("フロー:"))
        flow_row.addWidget(self._combo_flow, 1)
        flow_row.addWidget(self._btn_flow)
        lay.addLayout(flow_row)

        self._lbl_run_status = QLabel("待機中")
        self._lbl_run_status.setStyleSheet("color:#555; font-size:12px;")
        self._lbl_run_status.setWordWrap(True)
        lay.addWidget(self._lbl_run_status)

        lay.addWidget(QLabel("スケジュール:"))
        self._list_sched = QListWidget()
        self._list_sched.setStyleSheet("font-size:12px;")
        lay.addWidget(self._list_sched, 1)

        self._lbl_next_sched = QLabel("")
        self._lbl_next_sched.setStyleSheet("color:#1565c0; font-size:12px;")
        self._lbl_next_sched.setWordWrap(True)
        lay.addWidget(self._lbl_next_sched)
        return w

    # ---- テストタブ（カーソル移動 / クリック）
    def _build_tab_test(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 8, 6, 6)
        lay.setSpacing(8)

        # 共通の座標入力
        coord_grp = QGroupBox("目標座標 (絶対 px)")
        coord_lay = QHBoxLayout(coord_grp)
        coord_lay.setContentsMargins(8, 6, 8, 6)
        self._inp_test_x = QLineEdit()
        self._inp_test_x.setValidator(QIntValidator(-10000, 10000, self))
        self._inp_test_x.setFixedWidth(64)
        self._inp_test_y = QLineEdit()
        self._inp_test_y.setValidator(QIntValidator(-10000, 10000, self))
        self._inp_test_y.setFixedWidth(64)
        self._btn_test_getpos = QPushButton("現在位置")
        self._btn_test_getpos.setToolTip("マウスカーソルの現在位置を取得")
        self._btn_test_getpos.clicked.connect(self._fill_cursor_pos)
        self._btn_test_capture = QPushButton("クリック取得")
        self._btn_test_capture.setToolTip("次にクリックした位置を取得（10秒以内・ESCで中断）")
        self._btn_test_capture.clicked.connect(self._start_capture_click)
        coord_lay.addWidget(QLabel("X:"))
        coord_lay.addWidget(self._inp_test_x)
        coord_lay.addWidget(QLabel("Y:"))
        coord_lay.addWidget(self._inp_test_y)
        coord_lay.addWidget(self._btn_test_getpos)
        coord_lay.addWidget(self._btn_test_capture)
        coord_lay.addStretch(1)
        lay.addWidget(coord_grp)

        # 移動モード（滑らか / ジャンプ）
        mode_grp = QGroupBox("移動モード")
        mode_lay = QHBoxLayout(mode_grp)
        mode_lay.setContentsMargins(8, 6, 8, 6)
        self._rb_mode_smooth = QRadioButton("滑らか (HID 相対)")
        self._rb_mode_jump   = QRadioButton("ジャンプ (絶対座標)")
        self._rb_mode_smooth.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._rb_mode_smooth)
        self._mode_group.addButton(self._rb_mode_jump)
        mode_lay.addWidget(self._rb_mode_smooth)
        mode_lay.addWidget(self._rb_mode_jump)
        mode_lay.addStretch(1)
        lay.addWidget(mode_grp)

        # 速度スライダー（HID 移動・ドラッグ共通）
        speed_grp = QGroupBox("移動速度")
        speed_lay = QHBoxLayout(speed_grp)
        speed_lay.setContentsMargins(8, 6, 8, 6)
        self._slider_speed = QSlider(Qt.Horizontal)
        self._slider_speed.setRange(1, 10)
        self._slider_speed.setValue(5)
        self._slider_speed.setTickPosition(QSlider.TicksBelow)
        self._slider_speed.setTickInterval(1)
        self._slider_speed.valueChanged.connect(self._on_speed_changed)
        self._lbl_speed = QLabel()
        self._lbl_speed.setMinimumWidth(110)
        speed_lay.addWidget(QLabel("遅"))
        speed_lay.addWidget(self._slider_speed, 1)
        speed_lay.addWidget(QLabel("速"))
        speed_lay.addWidget(self._lbl_speed)
        lay.addWidget(speed_grp)
        self._on_speed_changed(5)  # 初期表示

        # カーソル移動
        move_grp = QGroupBox("カーソル移動 (Pico HID + 誤差補正)")
        move_lay = QVBoxLayout(move_grp)
        move_lay.setContentsMargins(8, 6, 8, 6)
        move_lay.setSpacing(4)
        self._btn_test_pico_move = QPushButton("HID 移動")
        self._btn_test_pico_move.clicked.connect(self._test_pico_move)
        move_lay.addWidget(self._btn_test_pico_move)
        self._btn_test_calibrate = QPushButton("Pico キャリブレーション")
        self._btn_test_calibrate.clicked.connect(self._test_calibrate)
        move_lay.addWidget(self._btn_test_calibrate)
        lay.addWidget(move_grp)

        # クリック
        click_grp = QGroupBox("クリック (Pico HID)")
        click_lay = QHBoxLayout(click_grp)
        click_lay.setContentsMargins(8, 6, 8, 6)
        self._btn_test_lclick = QPushButton("左クリック")
        self._btn_test_lclick.clicked.connect(lambda: self._test_click("L"))
        self._btn_test_rclick = QPushButton("右クリック")
        self._btn_test_rclick.clicked.connect(lambda: self._test_click("R"))
        click_lay.addWidget(self._btn_test_lclick)
        click_lay.addWidget(self._btn_test_rclick)
        lay.addWidget(click_grp)

        # ドラッグ
        drag_grp = QGroupBox("ドラッグ (Pico HID)")
        drag_lay = QVBoxLayout(drag_grp)
        drag_lay.setContentsMargins(8, 6, 8, 6)
        drag_lay.setSpacing(4)

        s_row = QHBoxLayout()
        s_row.addWidget(QLabel("開始 X:"))
        self._inp_drag_sx = QLineEdit()
        self._inp_drag_sx.setValidator(QIntValidator(-10000, 10000, self))
        self._inp_drag_sx.setFixedWidth(60)
        s_row.addWidget(self._inp_drag_sx)
        s_row.addWidget(QLabel("Y:"))
        self._inp_drag_sy = QLineEdit()
        self._inp_drag_sy.setValidator(QIntValidator(-10000, 10000, self))
        self._inp_drag_sy.setFixedWidth(60)
        s_row.addWidget(self._inp_drag_sy)
        btn_copy_s = QPushButton("↑ X/Y を開始へ")
        btn_copy_s.setToolTip("上の目標 X/Y をドラッグ開始へコピー")
        btn_copy_s.clicked.connect(self._copy_xy_to_drag_start)
        s_row.addWidget(btn_copy_s)
        s_row.addStretch(1)
        drag_lay.addLayout(s_row)

        e_row = QHBoxLayout()
        e_row.addWidget(QLabel("終了 X:"))
        self._inp_drag_ex = QLineEdit()
        self._inp_drag_ex.setValidator(QIntValidator(-10000, 10000, self))
        self._inp_drag_ex.setFixedWidth(60)
        e_row.addWidget(self._inp_drag_ex)
        e_row.addWidget(QLabel("Y:"))
        self._inp_drag_ey = QLineEdit()
        self._inp_drag_ey.setValidator(QIntValidator(-10000, 10000, self))
        self._inp_drag_ey.setFixedWidth(60)
        e_row.addWidget(self._inp_drag_ey)
        btn_copy_e = QPushButton("↑ X/Y を終了へ")
        btn_copy_e.setToolTip("上の目標 X/Y をドラッグ終了へコピー")
        btn_copy_e.clicked.connect(self._copy_xy_to_drag_end)
        e_row.addWidget(btn_copy_e)
        e_row.addStretch(1)
        drag_lay.addLayout(e_row)

        self._btn_test_drag = QPushButton("左ドラッグ (開始 → 終了)")
        self._btn_test_drag.clicked.connect(self._test_drag)
        drag_lay.addWidget(self._btn_test_drag)
        lay.addWidget(drag_grp)

        # ログ
        lay.addWidget(QLabel("実行結果:"))
        self._test_log = QTextEdit()
        self._test_log.setReadOnly(True)
        self._test_log.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 12px;"
        )
        lay.addWidget(self._test_log, 1)
        return w

    # ---- 見張りタブ: ウォッチャー一覧 + 編集起動（別ウィンドウ）
    def _build_tab_watcher(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 8, 6, 6)
        lay.setSpacing(6)

        lay.addWidget(QLabel("ウォッチャー一覧（チェックで有効／無効切替）:"))
        self._list_watchers = QListWidget()
        self._list_watchers.itemDoubleClicked.connect(
            lambda _i: self._open_watcher_editor()
        )
        self._list_watchers.itemChanged.connect(self._on_watcher_item_changed)
        lay.addWidget(self._list_watchers, 1)

        btn_row = QHBoxLayout()
        btn_new  = QPushButton("新規")
        btn_new.clicked.connect(self._new_watcher)
        btn_edit = QPushButton("編集…")
        btn_edit.clicked.connect(self._open_watcher_editor)
        btn_del  = QPushButton("削除")
        btn_del.clicked.connect(self._delete_watcher)
        btn_row.addWidget(btn_new)
        btn_row.addWidget(btn_edit)
        btn_row.addWidget(btn_del)
        lay.addLayout(btn_row)

        btn_refresh = QPushButton("一覧更新")
        btn_refresh.clicked.connect(self._reload_watchers_list)
        lay.addWidget(btn_refresh)

        hint = QLabel(
            "編集は別ウィンドウで開きます。"
            "現フェーズではフロー実行と連動せず、編集画面内の単独テストで動作確認。"
        )
        hint.setStyleSheet("color:#666; font-size:11px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self._reload_watchers_list()
        self._watcher_editors: list[WatcherEditorWindow] = []
        return w

    # ---- 作成タブ: シーン一覧 + 編集起動（別ウィンドウ）
    def _build_tab_editor(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 8, 6, 6)
        lay.setSpacing(6)

        lay.addWidget(QLabel("シーン一覧:"))
        self._list_scenes = QListWidget()
        self._list_scenes.itemDoubleClicked.connect(lambda _i: self._open_scene_editor())
        lay.addWidget(self._list_scenes, 1)

        btn_row = QHBoxLayout()
        btn_new  = QPushButton("新規")
        btn_new.clicked.connect(self._new_scene)
        btn_edit = QPushButton("編集…")
        btn_edit.clicked.connect(self._open_scene_editor)
        btn_dup  = QPushButton("複製")
        btn_dup.clicked.connect(self._duplicate_scene)
        btn_del  = QPushButton("削除")
        btn_del.clicked.connect(self._delete_scene)
        btn_row.addWidget(btn_new)
        btn_row.addWidget(btn_edit)
        btn_row.addWidget(btn_dup)
        btn_row.addWidget(btn_del)
        lay.addLayout(btn_row)

        btn_refresh = QPushButton("一覧更新")
        btn_refresh.clicked.connect(self._reload_scenes_list)
        lay.addWidget(btn_refresh)

        hint = QLabel(
            "編集は別ウィンドウ（大きめ）で開きます。"
            "対象ウィンドウはヘッダーの設定が引き継がれます。"
        )
        hint.setStyleSheet("color:#666; font-size:11px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self._reload_scenes_list()
        # 編集ウィンドウへの参照を保持（GC で閉じないように）
        self._scene_editors: list[SceneEditorWindow] = []
        return w

    # ---- シーン一覧の操作（作成タブ）
    def _reload_scenes_list(self) -> None:
        self._list_scenes.clear()
        if not os.path.isdir(SCENES_DIR):
            return
        for fname in sorted(os.listdir(SCENES_DIR)):
            if fname.endswith(".json"):
                item = QListWidgetItem(fname)
                item.setData(Qt.UserRole, os.path.join(SCENES_DIR, fname))
                self._list_scenes.addItem(item)

    def _selected_scene_path(self) -> str | None:
        item = self._list_scenes.currentItem()
        if item is None:
            return None
        return item.data(Qt.UserRole)

    def _open_scene_editor(self) -> None:
        path = self._selected_scene_path()
        title = self._settings.get("window_title", "")
        win = SceneEditorWindow(
            path, window_title=title, mouse_provider=lambda: self._mouse,
        )
        win.saved.connect(lambda _p: self._reload_scenes_list())
        win.closed.connect(self._on_scene_editor_closed)
        self._scene_editors.append(win)
        win.show()

    def _new_scene(self) -> None:
        title = self._settings.get("window_title", "")
        win = SceneEditorWindow(
            None, window_title=title, mouse_provider=lambda: self._mouse,
        )
        win.saved.connect(lambda _p: self._reload_scenes_list())
        win.closed.connect(self._on_scene_editor_closed)
        self._scene_editors.append(win)
        win.show()

    def _on_scene_editor_closed(self, win) -> None:
        if win in self._scene_editors:
            self._scene_editors.remove(win)
        self._reload_scenes_list()

    def _duplicate_scene(self) -> None:
        path = self._selected_scene_path()
        if not path or not os.path.exists(path):
            return
        from shutil import copyfile
        base = os.path.splitext(os.path.basename(path))[0]
        new_path = os.path.join(SCENES_DIR, f"{base}_copy.json")
        idx = 2
        while os.path.exists(new_path):
            new_path = os.path.join(SCENES_DIR, f"{base}_copy{idx}.json")
            idx += 1
        copyfile(path, new_path)
        self._reload_scenes_list()

    def _delete_scene(self) -> None:
        path = self._selected_scene_path()
        if not path or not os.path.exists(path):
            return
        from PySide6.QtWidgets import QMessageBox
        if QMessageBox.question(
            self, "削除確認",
            f"{os.path.basename(path)} を削除しますか？",
        ) != QMessageBox.Yes:
            return
        try:
            os.remove(path)
        except Exception as e:
            QMessageBox.warning(self, "削除失敗", str(e))
            return
        self._reload_scenes_list()

    # ---- ウォッチャー一覧の操作（見張りタブ）
    def _reload_watchers_list(self) -> None:
        # 再描画中の itemChanged 連鎖を防ぐためシグナルブロック
        self._list_watchers.blockSignals(True)
        try:
            self._list_watchers.clear()
            if not os.path.isdir(WATCHERS_DIR):
                return
            for fname in sorted(os.listdir(WATCHERS_DIR)):
                if not fname.endswith(".json"):
                    continue
                path = os.path.join(WATCHERS_DIR, fname)
                try:
                    w = load_pc_watcher(path)
                except Exception:
                    continue
                label = f"{w.title or fname}  [{w.condition.type}]"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, path)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if w.enabled else Qt.Unchecked)
                self._list_watchers.addItem(item)
        finally:
            self._list_watchers.blockSignals(False)

    def _on_watcher_item_changed(self, item: QListWidgetItem) -> None:
        """一覧のチェックボックスで有効/無効を即トグルし JSON を更新する。"""
        path = item.data(Qt.UserRole)
        if not path or not os.path.exists(path):
            return
        new_enabled = (item.checkState() == Qt.Checked)
        try:
            w = load_pc_watcher(path)
            if w.enabled == new_enabled:
                return
            w.enabled = new_enabled
            save_pc_watcher(w, path)
            self._append_log(
                f"ウォッチャー {'✓有効' if new_enabled else '✗無効'}: {w.title}"
            )
        except Exception as e:
            self._append_log(f"⚠ 切替失敗: {e}")

    def _selected_watcher_path(self) -> str | None:
        item = self._list_watchers.currentItem()
        if item is None:
            return None
        return item.data(Qt.UserRole)

    def _open_watcher_editor(self) -> None:
        path = self._selected_watcher_path()
        title = self._settings.get("window_title", "")
        win = WatcherEditorWindow(path, window_title=title)
        win.saved.connect(lambda _p: self._reload_watchers_list())
        win.closed.connect(self._on_watcher_editor_closed)
        self._watcher_editors.append(win)
        win.show()

    def _new_watcher(self) -> None:
        title = self._settings.get("window_title", "")
        win = WatcherEditorWindow(None, window_title=title)
        win.saved.connect(lambda _p: self._reload_watchers_list())
        win.closed.connect(self._on_watcher_editor_closed)
        self._watcher_editors.append(win)
        win.show()

    def _on_watcher_editor_closed(self, win) -> None:
        if win in self._watcher_editors:
            self._watcher_editors.remove(win)
        self._reload_watchers_list()

    def _delete_watcher(self) -> None:
        path = self._selected_watcher_path()
        if not path or not os.path.exists(path):
            return
        from PySide6.QtWidgets import QMessageBox
        if QMessageBox.question(
            self, "削除確認",
            f"{os.path.basename(path)} を削除しますか？",
        ) != QMessageBox.Yes:
            return
        try:
            os.remove(path)
        except Exception as e:
            QMessageBox.warning(self, "削除失敗", str(e))
            return
        self._reload_watchers_list()

    # ---- ログタブ
    def _build_tab_log(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 8, 6, 6)
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 12px;"
        )
        lay.addWidget(self._log_box, 1)
        btn_row = QHBoxLayout()
        btn_clear = QPushButton("クリア")
        btn_clear.clicked.connect(self._log_box.clear)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_clear)
        lay.addLayout(btn_row)
        return w

    # ---- placeholder
    def _build_placeholder(self, title: str, msg: str) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 24, 12, 12)
        lbl_t = QLabel(title)
        lbl_t.setStyleSheet("font-size:14px; font-weight:bold; color:#666;")
        lbl_m = QLabel(msg)
        lbl_m.setStyleSheet("color:#888; font-size:12px;")
        lbl_m.setWordWrap(True)
        lay.addWidget(lbl_t)
        lay.addWidget(lbl_m)
        lay.addStretch(1)
        return w

    def _connect_signals(self) -> None:
        self._runner.log_message.connect(self._append_log)
        self._runner.state_changed.connect(self._on_state_changed)
        self._runner.next_schedule_changed.connect(self._lbl_next_sched.setText)
        self._runner.scene_started.connect(self._on_scene_started)
        self._runner.step_updated.connect(self._on_step_updated)

    # ---------------------------------------------------------------- 設定復元
    def _purge_old_logs(self) -> None:
        retain = int(self._settings.get("log_retain_days", DEFAULT_LOG_RETAIN_DAYS))
        removed = purge_old_logs(retain)
        if removed:
            self._append_log(f"ログ整理: {removed} 日分の古いファイルを削除")

    def _setup_tesseract(self) -> None:
        """Tesseract OCR のパスを pytesseract に設定する（設定優先 → 自動検出）。

        未検出でもエラーにはせず、ログに警告するのみ。
        OCR を使う機能（ウォッチャーの ocr_number、経験値メーター）で初めて影響する。
        """
        cmd = (self._settings.get("tesseract_cmd") or "").strip()
        if cmd and os.path.isfile(cmd):
            apply_tesseract_path(cmd)
            self._append_log(f"Tesseract: {cmd}")
            return
        found = detect_tesseract()
        if found:
            apply_tesseract_path(found)
            self._settings["tesseract_cmd"] = found
            save_settings(self._settings)
            self._append_log(f"Tesseract 自動検出: {found}")
        else:
            self._append_log("⚠ Tesseract が見つかりません — OCR 機能は使えません")

    def _restore_settings(self) -> None:
        title = self._settings.get("window_title", "")
        if title:
            self._runner.set_window_title(title)
            self._update_win_label(title)

        last_flow = self._settings.get("last_flow", "")
        if last_flow:
            for i in range(self._combo_flow.count()):
                if self._combo_flow.itemData(i) == last_flow:
                    self._combo_flow.setCurrentIndex(i)
                    break

    # ---------------------------------------------------------------- フロー一覧
    def _load_flows_list(self) -> None:
        self._combo_flow.blockSignals(True)
        self._combo_flow.clear()
        if os.path.isdir(FLOWS_DIR):
            for fname in sorted(os.listdir(FLOWS_DIR)):
                if fname.endswith(".json"):
                    self._combo_flow.addItem(fname, fname)
        self._combo_flow.blockSignals(False)

    def _on_flow_selected(self, idx: int) -> None:
        fname = self._combo_flow.itemData(idx)
        if not fname:
            return
        path = os.path.join(FLOWS_DIR, fname)
        if not os.path.exists(path):
            return
        try:
            flow = self._runner.load_flow(path)
            self._update_schedule_list(flow.schedule)
            self._settings["last_flow"] = fname
            save_settings(self._settings)
        except Exception as e:
            self._append_log(f"フロー読込エラー: {e}")

    def _update_schedule_list(self, schedule) -> None:
        self._list_sched.clear()
        for entry in schedule:
            if not entry.enabled:
                continue
            scenes = entry_scenes(entry)
            name = os.path.splitext(scenes[0])[0] if scenes else entry.target
            if entry.repeat == "weekly" and entry.days:
                days_str = "・".join(DAY_NAMES[d] for d in entry.days)
            elif entry.repeat == "daily":
                days_str = "毎日"
            elif entry.repeat == "once":
                days_str = entry.date
            else:
                days_str = ""
            self._list_sched.addItem(f"{entry.time}  {name}  ({days_str})")

    # ---------------------------------------------------------------- ウィンドウ選択
    def _pick_window(self) -> None:
        cur = self._settings.get("window_title", "")
        dlg = WindowPickerDialog(cur, self)
        if dlg.exec():
            title = dlg.selected_title()
            self._settings["window_title"] = title
            save_settings(self._settings)
            self._runner.set_window_title(title)
            self._update_win_label(title)

    def _update_win_label(self, title: str) -> None:
        if not title:
            self._lbl_win.setText("未設定")
            self._lbl_win.setStyleSheet("color:#c62828;")
        elif find_hwnd_by_title(title):
            self._lbl_win.setText(f"✓ {title}")
            self._lbl_win.setStyleSheet("color:#2e7d32;")
        else:
            self._lbl_win.setText(f"⚠ {title}（見つかりません）")
            self._lbl_win.setStyleSheet("color:#ef6c00;")

    # ---------------------------------------------------------------- Pico 接続
    def _auto_connect_pico(self) -> None:
        if PicoMouse is None or find_pico_port is None:
            return
        try:
            if find_pico_port():
                self._do_connect_pico()
        except Exception:
            pass

    def _connect_pico(self) -> None:
        self._do_connect_pico()

    def _do_connect_pico(self) -> None:
        if PicoMouse is None:
            self._lbl_pico.setText("✗ pico_mouse が見つかりません")
            self._lbl_pico.setStyleSheet("color:#c62828;")
            self._set_test_enabled(False)
            return

        if self._mouse:
            try:
                self._mouse.close()
            except Exception:
                pass
            self._mouse = None
            self._set_test_enabled(False)

        try:
            self._mouse = PicoMouse()
            self._runner.set_mouse(self._mouse)
            self._lbl_pico.setText(f"✓ 接続済 {self._mouse.port}")
            self._lbl_pico.setStyleSheet("color:#2e7d32;")
            self._btn_pico.setText("再接続")
            self._append_log(f"Pico 接続: {self._mouse.port}")
            self._set_test_enabled(True)
        except Exception as e:
            self._lbl_pico.setText(f"✗ 接続失敗: {e}")
            self._lbl_pico.setStyleSheet("color:#c62828;")
            self._append_log(f"Pico 接続エラー: {e}")
            self._set_test_enabled(False)

    # ---------------------------------------------------------------- フロー開始/停止
    def _toggle_flow(self) -> None:
        if self._runner.is_running:
            self._runner.stop()
        else:
            if self._runner._flow is None:
                self._append_log("フローが選択されていません")
                return
            self._runner.start()

    def _on_state_changed(self, state: str) -> None:
        if state == "idle":
            self._btn_flow.setText("開始")
            self._btn_flow.setStyleSheet("")
            self._lbl_run_status.setText("待機中")
        elif state == "running":
            self._btn_flow.setText("停止")
            self._btn_flow.setStyleSheet(
                "QPushButton{background:#c62828;color:white;font-weight:bold;}"
            )

    def _on_scene_started(self, name: str, step: int, total: int) -> None:
        self._lbl_run_status.setText(f"実行中: {name}  ステップ {step}/{total}")

    def _on_step_updated(self, step: int, total: int) -> None:
        scene = self._runner.current_scene
        self._lbl_run_status.setText(f"実行中: {scene}  ステップ {step}/{total}")

    # ---------------------------------------------------------------- テストタブ
    def _get_test_xy(self) -> tuple[int, int] | None:
        try:
            x = int(self._inp_test_x.text())
            y = int(self._inp_test_y.text())
        except ValueError:
            self._test_log_append("⚠ X / Y を整数で入力してください")
            return None
        return x, y

    def _fill_cursor_pos(self) -> None:
        if not self._mouse:
            return
        x, y = self._mouse.get_cursor_pos()
        self._inp_test_x.setText(str(x))
        self._inp_test_y.setText(str(y))
        self._test_log_append(f"現在位置: ({x}, {y})")

    # ---- クリック座標キャプチャ ----
    def _start_capture_click(self) -> None:
        """全画面オーバーレイを表示し、複数のクリック位置を連続取得する。

        ゲームウィンドウがマウスをキャプチャしていても、オーバーレイが上に
        被さるのでクリックを横取りできる。右クリック・ESC で取得モード終了。
        """
        if self._capture_overlay is not None:
            # 連打されたら終了扱い
            self._capture_overlay.close()
            return
        self._test_log_append(
            "クリック取得: 左クリックで位置を記録（右クリック・ESC で終了）"
        )
        self._capture_count = 0
        ov = _ClickCaptureOverlay()
        ov.clicked.connect(self._on_capture_clicked)
        ov.finished.connect(self._on_capture_finished)
        self._capture_overlay = ov
        ov.show()

    def _on_capture_clicked(self, x: int, y: int) -> None:
        self._capture_count += 1
        self._inp_test_x.setText(str(x))
        self._inp_test_y.setText(str(y))
        self._test_log_append(f"クリック取得 #{self._capture_count}: ({x}, {y})")

    def _on_capture_finished(self) -> None:
        n = self._capture_count
        if n == 0:
            self._test_log_append("クリック取得: 中断（取得なし）")
        else:
            self._test_log_append(f"クリック取得: 終了（計 {n} 件）")
        self._capture_overlay = None

    def _get_speed_params(self) -> tuple[int, float]:
        """速度スライダーから (max_step, delay) を返す。

        遅い側を広げて、Windows のポインター加速の影響を受けにくいレンジに届くようにした:
        speed=1  (最遅): max_step=2,  delay=0.080s → 約   25 px/s（加速ほぼなし）
        speed=5  (中) : max_step=18, delay=0.045s → 約  400 px/s
        speed=10 (最速): max_step=40, delay=0.005s → 約 8000 px/s
        """
        speed = self._slider_speed.value()
        # 1→2 / 10→40 を線形に
        max_step = max(1, int(round(2 + (speed - 1) * 38 / 9)))
        # 1→0.080s / 10→0.005s を線形に
        delay = max(0.001, 0.080 - (speed - 1) * 0.075 / 9)
        return max_step, delay

    def _on_speed_changed(self, _v: int) -> None:
        max_step, delay = self._get_speed_params()
        approx_pxs = int(max_step / max(delay, 0.001))
        self._lbl_speed.setText(f"{self._slider_speed.value()} ({approx_pxs} px/s)")

    def _test_pico_move(self) -> None:
        xy = self._get_test_xy()
        if xy is None:
            return
        if not self._mouse:
            self._test_log_append("⚠ Pico 未接続")
            return
        x, y = xy
        try:
            if self._rb_mode_jump.isChecked():
                # ジャンプ移動: SetCursorPos で絶対座標へ瞬時に
                self._mouse.move_cursor(x, y)
                fx, fy = self._mouse.get_cursor_pos()
                self._test_log_append(
                    f"ジャンプ移動 → ({fx}, {fy})  目標 ({x}, {y})  "
                    f"誤差 ({fx-x:+d}, {fy-y:+d})"
                )
                return
            # 滑らか移動: HID 相対 + フィードバック補正
            max_step, delay = self._get_speed_params()
            iters: list[str] = []

            def _on_iter(i: int, cx: int, cy: int, ex: int, ey: int) -> None:
                iters.append(f"  #{i+1} 位置({cx},{cy}) 残差({ex:+d},{ey:+d})")

            fx, fy = self._mouse.move_to_accurate(
                x, y, step=max_step, delay=delay, on_iter=_on_iter,
            )
            self._test_log_append(
                f"HID移動 → ({fx}, {fy})  目標 ({x}, {y})  "
                f"誤差 ({fx-x:+d}, {fy-y:+d})  補正 {len(iters)} 回"
            )
            for line in iters:
                self._test_log_append(line)
        except Exception as e:
            self._test_log_append(f"⚠ HID移動エラー: {e}")

    def _copy_xy_to_drag_start(self) -> None:
        self._inp_drag_sx.setText(self._inp_test_x.text())
        self._inp_drag_sy.setText(self._inp_test_y.text())

    def _copy_xy_to_drag_end(self) -> None:
        self._inp_drag_ex.setText(self._inp_test_x.text())
        self._inp_drag_ey.setText(self._inp_test_y.text())

    def _get_drag_xy(self) -> tuple[int, int, int, int] | None:
        try:
            sx = int(self._inp_drag_sx.text())
            sy = int(self._inp_drag_sy.text())
            ex = int(self._inp_drag_ex.text())
            ey = int(self._inp_drag_ey.text())
        except ValueError:
            self._test_log_append("⚠ ドラッグの開始/終了座標を整数で入力してください")
            return None
        return sx, sy, ex, ey

    # ドラッグ本体の固定速度（人手相当のゆっくり）
    _DRAG_MAX_STEP = 8
    _DRAG_DELAY    = 0.05   # 約 160 px/s
    _DRAG_PAUSE_MS = 200    # 開始位置到達後の一呼吸

    def _test_drag(self) -> None:
        """指定した開始座標から終了座標へ左ボタン押下のままドラッグする。

        移動モード:
            - 滑らか: HID 相対で開始位置（補正あり、スライダー速度）→ 一呼吸 → press
                     → 固定 160 px/s で終了へ → release
            - ジャンプ: SetCursorPos で開始位置に瞬間移動 → 一呼吸 → press
                       → SetCursorPos で終了位置に瞬間移動 → release
        """
        coords = self._get_drag_xy()
        if coords is None:
            return
        if not self._mouse:
            self._test_log_append("⚠ Pico 未接続")
            return
        sx, sy, ex, ey = coords

        if self._rb_mode_jump.isChecked():
            # ジャンプモード: 開始位置だけジャンプ、ドラッグ移動は必ず滑らか
            # （滑らか移動でないとゲームがドラッグとして反応しないため）
            try:
                # 1) 開始位置へジャンプ
                self._mouse.move_cursor(sx, sy)
                ax, ay = self._mouse.get_cursor_pos()
                # 2) 一呼吸
                time.sleep(self._DRAG_PAUSE_MS / 1000)
                # 3) 押下
                self._mouse.press("L")
                try:
                    # 4) 終了位置へ滑らかに移動（固定 160 px/s）
                    self._mouse.move_to(
                        ex, ey,
                        max_step=self._DRAG_MAX_STEP,
                        delay=self._DRAG_DELAY,
                    )
                    fx, fy = self._mouse.get_cursor_pos()
                finally:
                    try:
                        self._mouse.release("L")
                    except Exception:
                        pass
                self._test_log_append(
                    f"ジャンプ→滑らかドラッグ(L)  指定 ({sx},{sy})→({ex},{ey})  "
                    f"実 ({ax},{ay})→({fx},{fy})  "
                    f"開始誤差 ({ax-sx:+d},{ay-sy:+d})  "
                    f"終了誤差 ({fx-ex:+d},{fy-ey:+d})  "
                    f"ドラッグ速度=160 px/s"
                )
            except Exception as e:
                self._test_log_append(f"⚠ ドラッグエラー: {e}")
                try:
                    self._mouse.release()
                except Exception:
                    pass
            return

        # 滑らかモード
        max_step, delay = self._get_speed_params()
        try:
            start_iters: list[str] = []

            def _on_start(i: int, cx: int, cy: int, dxe: int, dye: int) -> None:
                start_iters.append(f"  start#{i+1} 位置({cx},{cy}) 残差({dxe:+d},{dye:+d})")

            ax, ay = self._mouse.move_to_accurate(
                sx, sy, step=max_step, delay=delay, on_iter=_on_start,
            )
            time.sleep(self._DRAG_PAUSE_MS / 1000)
            self._mouse.press("L")
            try:
                self._mouse.move_to(
                    ex, ey,
                    max_step=self._DRAG_MAX_STEP,
                    delay=self._DRAG_DELAY,
                )
                fx, fy = self._mouse.get_cursor_pos()
            finally:
                try:
                    self._mouse.release("L")
                except Exception:
                    pass
            self._test_log_append(
                f"ドラッグ(L)  指定 ({sx},{sy})→({ex},{ey})  "
                f"実 ({ax},{ay})→({fx},{fy})  "
                f"開始誤差 ({ax-sx:+d},{ay-sy:+d}) 補正{len(start_iters)}回  "
                f"終了誤差 ({fx-ex:+d},{fy-ey:+d})  "
                f"ドラッグ速度=160 px/s"
            )
            for line in start_iters:
                self._test_log_append(line)
        except Exception as e:
            self._test_log_append(f"⚠ ドラッグエラー: {e}")
            try:
                self._mouse.release()
            except Exception:
                pass

    def _test_calibrate(self) -> None:
        if not self._mouse:
            self._test_log_append("⚠ Pico 未接続")
            return
        try:
            scale = self._mouse.calibrate()
            self._test_log_append(f"キャリブレーション完了  speed_scale = {scale:.4f}")
        except Exception as e:
            self._test_log_append(f"⚠ キャリブレーションエラー: {e}")

    def _test_click(self, button: str) -> None:
        xy = self._get_test_xy()
        if xy is None:
            return
        if not self._mouse:
            self._test_log_append("⚠ Pico 未接続")
            return
        x, y = xy
        try:
            self._mouse.click(x, y, button)
            self._test_log_append(f"クリック ({button}) → ({x}, {y})")
        except Exception as e:
            self._test_log_append(f"⚠ クリックエラー: {e}")

    def _test_log_append(self, msg: str) -> None:
        self._test_log.append(msg)
        sb = self._test_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ---------------------------------------------------------------- 定期更新
    def _refresh_status(self) -> None:
        title = self._settings.get("window_title", "")
        self._update_win_label(title)

    # ---------------------------------------------------------------- ログ
    def _append_log(self, msg: str) -> None:
        self._log_box.append(msg)
        sb = self._log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ---------------------------------------------------------------- 終了
    def closeEvent(self, e) -> None:  # noqa: N802
        self._runner.stop()
        if self._mouse:
            try:
                self._mouse.close()
            except Exception:
                pass
        super().closeEvent(e)


class _ClickCaptureOverlay(QWidget):
    """全画面の透明オーバーレイ。左クリックで座標を連続取得する。

    - 左クリック: クリック位置を `clicked` シグナルで通知（オーバーレイは閉じない）
    - 右クリック / ESC: `finished` シグナル → 閉じる
    """

    clicked  = Signal(int, int)   # クリック位置 (x, y)
    finished = Signal()           # 取得モード終了

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)

        # 仮想スクリーン全体を覆う
        vg = QApplication.primaryScreen().virtualGeometry()
        self.setGeometry(vg)

        self._count = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.addStretch(1)
        self._lbl = QLabel()
        self._lbl.setAlignment(Qt.AlignCenter)
        self._lbl.setStyleSheet(
            "background:rgba(0,0,0,180); color:white; padding:10px 20px;"
            "font-size:13px; border-radius:6px;"
        )
        self._update_label()
        lay.addWidget(self._lbl, 0, Qt.AlignHCenter)
        lay.addStretch(1)

    def _update_label(self) -> None:
        self._lbl.setText(
            f"左クリック=取得 ({self._count}件)  /  右クリック・ESC=終了"
        )

    def paintEvent(self, e) -> None:  # noqa: N802
        # 薄く塗ってオーバーレイの存在を分かりやすく
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 40))

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            p = e.globalPosition().toPoint()
            self._count += 1
            self._update_label()
            self.clicked.emit(p.x(), p.y())
        elif e.button() == Qt.RightButton:
            self.finished.emit()
            self.close()

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e.key() == Qt.Key_Escape:
            self.finished.emit()
            self.close()

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self.activateWindow()
        self.setFocus()


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)

    # 全体フォントを少し大きく＋太く（Segoe UI Medium 10pt）
    f = app.font()
    f.setPointSize(10)
    f.setWeight(QFont.Medium)
    app.setFont(f)

    settings = load_settings()
    win = PcFlowWindow(settings)
    win.show()
    sys.exit(app.exec())
