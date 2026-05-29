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

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIntValidator, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .exp_meter import CURRENT_WINDOW, ExpMeter
from .overlay import OverlayWindow as ExpOverlayWindow
from .logger import DEFAULT_LOG_RETAIN_DAYS, purge_old_logs, write_log
from .pc_flow import (
    DAY_NAMES,
    FLOWS_DIR,
    PcFlowRunner,
    ScheduleEntry,
    entry_scenes,
    load_pc_flow,
    save_pc_flow,
)
from .pc_flow_editor import FlowEditorWindow, _EntryDialog
from .foreground_dialog import ForegroundConfirmDialog
from .flow_overlay import FlowOverlay
from .pc_scene import SCENES_DIR, load_pc_scene
from .pc_scene_editor import SceneEditorWindow
from .pc_watcher import WATCHERS_DIR, load_pc_watcher, save_pc_watcher
from .pc_watcher_editor import WatcherEditorWindow
from .watcher_counts import load_counts as load_watcher_counts
from .notify import send_google_chat
from .recorder import RECORDINGS_DIR, WindowRecorder
from .widgets import ReorderableListWidget
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

    _test_log_signal = Signal(str)   # 別スレッドからテストログを安全に表示するため
    _tr_chat_signal  = Signal(str)   # 翻訳タブのチャット欄追記
    _tr_user_signal  = Signal(str)   # 翻訳タブのユーザー欄追記
    _tr_user_busy_signal = Signal(bool)   # ユーザー翻訳ボタンの有効/無効

    def __init__(self, settings: dict) -> None:
        super().__init__()
        self._settings = settings
        self._mouse: "PicoMouse | None" = None
        self._runner = PcFlowRunner()
        # 経験値計測（タブで使う）。設定とサンプル列は exp_meter.json で永続化される
        self._exp_meter = ExpMeter()
        self._exp_overlay: "ExpOverlayWindow | None" = None

        self.setWindowTitle("PC フロー制御")
        self.setMinimumWidth(360)
        # 画面の利用可能高さに収める（タスクバー等を除いた領域）。
        # タイトルバー分のマージンを引いて、ウィンドウ枠まで含めて画面に収まるようにする。
        screen = QApplication.primaryScreen()
        avail_h = screen.availableGeometry().height() if screen else 1000
        max_h = max(400, avail_h - 60)
        target_h = min(900, max(600, max_h))
        self.resize(480, target_h)
        self.setMaximumHeight(max_h)

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
        self._tabs.addTab(self._build_tab_run(),         "実行")
        self._tabs.addTab(self._build_tab_test(),        "テスト")
        self._tabs.addTab(self._build_tab_watcher(),     "見張り")
        self._tabs.addTab(self._build_tab_editor(),      "作成")
        self._tabs.addTab(self._build_tab_record(),      "録画")
        self._tabs.addTab(self._build_tab_exp_meter(),   "経験値")
        self._tabs.addTab(self._build_tab_translation(), "翻訳")
        self._tabs.addTab(self._build_tab_log(),         "ログ")
        outer.addWidget(self._tabs, 1)

        # 下部: 設定・終了ボタン
        quit_row = QHBoxLayout()
        btn_settings = QPushButton("⚙ 設定")
        btn_settings.setFixedWidth(90)
        btn_settings.setToolTip("Google Chat 通知の Webhook 設定など")
        btn_settings.clicked.connect(self._open_settings)
        quit_row.addWidget(btn_settings)
        quit_row.addStretch(1)
        btn_quit = QPushButton("✕ 終了")
        btn_quit.setFixedWidth(90)
        btn_quit.setToolTip(
            "メインウィンドウと開いている編集ウィンドウを全て閉じる"
        )
        btn_quit.clicked.connect(self.close)
        quit_row.addWidget(btn_quit)
        outer.addLayout(quit_row)

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
            self._btn_seq_hid,
            self._btn_seq_park,
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
        self._btn_flow = QPushButton("▶ 開始")
        self._btn_flow.setFixedWidth(72)
        self._btn_flow.setStyleSheet(
            "QPushButton{background:#2e7d32;color:white;font-weight:bold;}"
        )
        self._btn_flow.clicked.connect(self._toggle_flow)
        flow_row.addWidget(QLabel("フロー:"))
        flow_row.addWidget(self._combo_flow, 1)
        flow_row.addWidget(self._btn_flow)
        lay.addLayout(flow_row)

        self._lbl_run_status = QLabel("待機中")
        self._lbl_run_status.setStyleSheet("color:#555; font-size:12px;")
        self._lbl_run_status.setWordWrap(True)
        lay.addWidget(self._lbl_run_status)

        # 曜日ページャー
        day_row = QHBoxLayout()
        self._btn_day_prev = QPushButton("◀")
        self._btn_day_prev.setFixedWidth(32)
        self._btn_day_prev.clicked.connect(self._show_prev_day)
        self._btn_day_next = QPushButton("▶")
        self._btn_day_next.setFixedWidth(32)
        self._btn_day_next.clicked.connect(self._show_next_day)
        self._btn_day_today = QPushButton("今日")
        self._btn_day_today.setFixedWidth(48)
        self._btn_day_today.clicked.connect(self._show_today)
        self._lbl_day = QLabel()
        self._lbl_day.setAlignment(Qt.AlignCenter)
        self._lbl_day.setStyleSheet("font-weight:bold;")
        day_row.addWidget(self._btn_day_prev)
        day_row.addWidget(self._lbl_day, 1)
        day_row.addWidget(self._btn_day_next)
        day_row.addWidget(self._btn_day_today)
        lay.addLayout(day_row)

        # 1時間刻み × 1列の本日フロー表（現在時刻に赤線、全体フローと同じ動作）
        self._sched_table = _RunDayTable()
        self._sched_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._sched_table.customContextMenuRequested.connect(
            self._on_sched_table_context_menu
        )
        self._sched_table.cellDoubleClicked.connect(
            self._on_sched_table_double_clicked
        )
        lay.addWidget(self._sched_table, 1)

        sched_hint = QLabel(
            "ダブルクリックで編集 / 右クリックで単発実行できます（時刻は 1 分単位）。"
        )
        sched_hint.setStyleSheet("color:#666; font-size:11px;")
        lay.addWidget(sched_hint)

        self._lbl_next_sched = QLabel("")
        self._lbl_next_sched.setStyleSheet("color:#1565c0; font-size:12px;")
        self._lbl_next_sched.setWordWrap(True)
        lay.addWidget(self._lbl_next_sched)

        btn_edit_flow = QPushButton("📅 フロー全体編集…")
        btn_edit_flow.clicked.connect(self._open_flow_editor)
        lay.addWidget(btn_edit_flow)

        self._displayed_weekday = datetime.now().weekday()
        self._update_day_label()
        return w

    # ---- テストタブ（カーソル移動 / クリック）
    def _build_tab_test(self) -> QWidget:
        # 上下分割: コントロール群（縦スクロール可） / 実行結果ログ。
        # ユーザーがスプリッタをドラッグして配分を変えられる。
        outer = QWidget()
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(6, 8, 6, 6)
        outer_lay.setSpacing(4)

        # 上部: コントロールを QScrollArea に詰める
        controls = QWidget()
        lay = QVBoxLayout(controls)
        lay.setContentsMargins(0, 0, 0, 0)
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

        # 3 点連続クリックテスト
        seq_grp = QGroupBox("連続クリック検証 (3 点を順次クリック)")
        seq_lay = QVBoxLayout(seq_grp)
        seq_lay.setContentsMargins(8, 6, 8, 6)
        seq_lay.setSpacing(4)
        seq_wait_row = QHBoxLayout()
        seq_wait_row.addWidget(QLabel("間隔:"))
        self._spin_seq_wait = QDoubleSpinBox()
        self._spin_seq_wait.setRange(0.1, 30.0)
        self._spin_seq_wait.setSingleStep(0.5)
        self._spin_seq_wait.setDecimals(1)
        self._spin_seq_wait.setValue(1.5)
        self._spin_seq_wait.setSuffix(" 秒")
        self._spin_seq_wait.setFixedWidth(80)
        seq_wait_row.addWidget(self._spin_seq_wait)
        seq_wait_row.addStretch(1)
        seq_lay.addLayout(seq_wait_row)

        # 3 点それぞれの座標入力（このセクション専用、他のフィールドと独立）
        self._seq_inputs: list[tuple[QLineEdit, QLineEdit]] = []
        for idx in range(1, 4):
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{idx}点目  X:"))
            ix = QLineEdit()
            ix.setValidator(QIntValidator(-10000, 10000, self))
            ix.setFixedWidth(60)
            iy = QLineEdit()
            iy.setValidator(QIntValidator(-10000, 10000, self))
            iy.setFixedWidth(60)
            row.addWidget(ix)
            row.addWidget(QLabel("Y:"))
            row.addWidget(iy)
            btn_cap = QPushButton("取得")
            btn_cap.setToolTip(f"次のクリック位置を {idx} 点目に記録（右クリック・ESC で中断）")
            btn_cap.clicked.connect(lambda _checked=False, i=idx: self._start_capture_seq(i))
            row.addWidget(btn_cap)
            row.addStretch(1)
            seq_lay.addLayout(row)
            self._seq_inputs.append((ix, iy))

        self._btn_seq_hid = QPushButton("A: HID 直接で 3 点連続クリック (click_at)")
        self._btn_seq_hid.setToolTip(
            "毎回 HID 相対移動で目標へ動かしてクリック。確実だが少し遅い。"
        )
        self._btn_seq_hid.clicked.connect(self._test_seq_hid)
        seq_lay.addWidget(self._btn_seq_hid)
        self._btn_seq_park = QPushButton(
            "B: 画面外パーク → SetCursorPos で 3 点連続クリック"
        )
        self._btn_seq_park.setToolTip(
            "ヘッダーで選んだ対象ウィンドウの外へ HID で逃がしてから "
            "SetCursorPos でジャンプ → CLICK。\n"
            "ゲーム内でカーソルが滞在中だと SetCursorPos がブロックされる仮説の検証用。"
        )
        self._btn_seq_park.clicked.connect(self._test_seq_park)
        seq_lay.addWidget(self._btn_seq_park)
        lay.addWidget(seq_grp)

        lay.addStretch(1)   # コントロール下部の余白

        # コントロールを縦スクロール可能なエリアに格納
        scroll = QScrollArea()
        scroll.setWidget(controls)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.NoFrame)

        # 下部: 実行結果ログ
        log_w = QWidget()
        log_lay = QVBoxLayout(log_w)
        log_lay.setContentsMargins(0, 0, 0, 0)
        log_lay.setSpacing(4)
        log_lay.addWidget(QLabel("実行結果:"))
        self._test_log = QTextEdit()
        self._test_log.setReadOnly(True)
        self._test_log.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 12px;"
        )
        log_lay.addWidget(self._test_log, 1)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(scroll)
        splitter.addWidget(log_w)
        # 初期は上 60% / 下 40%（ログを今までより広めに）
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([360, 280])

        outer_lay.addWidget(splitter, 1)
        return outer

    # ---- 見張りタブ: ウォッチャー一覧 + 編集起動（別ウィンドウ）
    def _build_tab_watcher(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 8, 6, 6)
        lay.setSpacing(6)

        lay.addWidget(QLabel("ウォッチャー一覧（チェックで有効／無効切替）:"))
        self._lbl_watcher_today = QLabel("本日の発火")
        self._lbl_watcher_today.setStyleSheet("color:#555; font-size:11px;")
        lay.addWidget(self._lbl_watcher_today)
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
            "フロー実行中はバックグラウンドで監視され、発火時に行が黄色く点滅します。"
            "本日の発火回数はアプリ再起動後も引き継がれ、深夜 0 時に自動リセットされます。"
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

        lay.addWidget(QLabel("シーン一覧（ドラッグで並べ替え可・使わないものは下へ）:"))
        self._list_scenes = ReorderableListWidget()
        self._list_scenes.setDragDropMode(QAbstractItemView.InternalMove)
        self._list_scenes.setDefaultDropAction(Qt.MoveAction)
        self._list_scenes.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list_scenes.itemDoubleClicked.connect(lambda _i: self._open_scene_editor())
        self._list_scenes.rows_reordered.connect(self._on_scenes_reordered)
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

        # 単発実行ボタン（緑、強調）
        self._btn_run_scene = QPushButton("▶ 選択シーンを実行")
        self._btn_run_scene.setStyleSheet(
            "QPushButton{background:#2e7d32;color:white;font-weight:bold;}"
        )
        self._btn_run_scene.setToolTip(
            "選択中のシーンを単発で実行します。スケジュール実行中は使えません。"
        )
        self._btn_run_scene.clicked.connect(self._run_selected_scene)
        lay.addWidget(self._btn_run_scene)

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
    _SCENE_ORDER_FILE = "_order.json"  # SCENES_DIR 配下に置く（"_" 始まりは一覧から除外）

    def _scene_order_path(self) -> str:
        return os.path.join(SCENES_DIR, self._SCENE_ORDER_FILE)

    def _load_scene_order(self) -> list[str]:
        path = self._scene_order_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(x) for x in data]
        except Exception as e:
            self._append_log(f"⚠ シーン並び順読込失敗: {e}")
        return []

    def _save_scene_order(self, order: list[str]) -> None:
        os.makedirs(SCENES_DIR, exist_ok=True)
        try:
            with open(self._scene_order_path(), "w", encoding="utf-8") as f:
                json.dump(order, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._append_log(f"⚠ シーン並び順保存失敗: {e}")

    def _ordered_scene_files(self) -> list[str]:
        """カスタム順序 → 残りはアルファベット順、で .json ファイル名を返す。

        '_' で始まるファイル（_order.json など内部用）は除外する。
        """
        if not os.path.isdir(SCENES_DIR):
            return []
        existing = sorted(
            f for f in os.listdir(SCENES_DIR)
            if f.endswith(".json") and not f.startswith("_")
        )
        existing_set = set(existing)
        out: list[str] = []
        seen: set[str] = set()
        for fname in self._load_scene_order():
            if fname in existing_set and fname not in seen:
                out.append(fname)
                seen.add(fname)
        for fname in existing:
            if fname not in seen:
                out.append(fname)
        return out

    def _reload_scenes_list(self) -> None:
        # rows_reordered と循環しないよう一旦シグナル停止
        self._list_scenes.blockSignals(True)
        try:
            self._list_scenes.clear()
            for fname in self._ordered_scene_files():
                path = os.path.join(SCENES_DIR, fname)
                # flow_target を読んで一覧表示のマークを切替
                try:
                    is_target = bool(load_pc_scene(path).flow_target)
                except Exception:
                    is_target = True  # 読めなければ安全側（候補扱い）
                mark = "🏁" if is_target else "🧩"
                item = QListWidgetItem(f"{mark} {fname}")
                item.setData(Qt.UserRole, path)
                if not is_target:
                    # 部品シーンはグレー文字で控えめに
                    item.setForeground(QBrush(QColor(140, 140, 140)))
                self._list_scenes.addItem(item)
        finally:
            self._list_scenes.blockSignals(False)

    def _on_scenes_reordered(self) -> None:
        # 表示文字列はマーク付きなので、Qt.UserRole に持たせたフルパスから
        # basename を取ってファイル名で並び順を保存する
        order: list[str] = []
        for i in range(self._list_scenes.count()):
            path = self._list_scenes.item(i).data(Qt.UserRole)
            if path:
                order.append(os.path.basename(path))
        self._save_scene_order(order)

    def _selected_scene_path(self) -> str | None:
        item = self._list_scenes.currentItem()
        if item is None:
            return None
        return item.data(Qt.UserRole)

    def _run_selected_scene(self) -> None:
        """作成タブで選択中のシーンを単発実行する。"""
        path = self._selected_scene_path()
        if not path:
            self._append_log("⚠ シーンが選択されていません")
            return
        if self._runner.is_busy:
            QMessageBox.information(
                self, "実行中",
                "スケジュール実行または単発実行が進行中です。先に停止してください。",
            )
            return
        if self._mouse is None:
            ans = QMessageBox.question(
                self, "Pico 未接続",
                "Pico マウス未接続です。tap/swipe 系のステップはスキップされます。\n"
                "続行しますか？",
            )
            if ans != QMessageBox.Yes:
                return
        # ランナーに対象ウィンドウ・マウスを反映してから実行
        self._runner.set_window_title(self._settings.get("window_title", ""))
        self._runner.set_mouse(self._mouse)
        fname = os.path.basename(path)
        if not self._runner.run_scene_async(fname):
            self._append_log("⚠ 単発実行を受け付けられませんでした（既に実行中？）")

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

    # ---- フロー編集（別ウィンドウ）
    def _open_flow_editor(self) -> None:
        # 現在選択中のフローを編集対象に
        path: str | None = None
        fname = self._combo_flow.currentData()
        if fname:
            candidate = os.path.join(FLOWS_DIR, fname)
            if os.path.exists(candidate):
                path = candidate
        if not hasattr(self, "_flow_editors"):
            self._flow_editors: list[FlowEditorWindow] = []
        win = FlowEditorWindow(path)
        win.saved.connect(self._on_flow_saved)
        win.applied.connect(self._on_flow_applied)
        win.closed.connect(self._on_flow_editor_closed)
        self._flow_editors.append(win)
        win.show()

    def _on_flow_saved(self, path: str) -> None:
        # 一覧を更新（ファイル保存のみ。実行中ランナーには触れない方針）
        # 選択中のフローが上書きされた場合でも、実行中なら旧スケジュールが走り続ける。
        # 即時反映が欲しい場合は編集側の「保存して反映」ボタンを使う。
        self._load_flows_list()
        if not self._runner.is_running:
            cur = self._combo_flow.currentData()
            if cur and os.path.basename(path) == cur:
                try:
                    flow = self._runner.load_flow(os.path.join(FLOWS_DIR, cur))
                    self._update_schedule_list(flow.schedule)
                except Exception as e:
                    self._append_log(f"フロー再ロード失敗: {e}")

    def _on_flow_applied(self, path: str) -> None:
        """編集側「保存して反映」: 実行中でも即時にランナーへ反映する。"""
        self._load_flows_list()
        cur = self._combo_flow.currentData()
        if not cur or os.path.basename(path) != cur:
            self._append_log(
                f"⚠ 「保存して反映」: 現在選択中のフローと違うため反映スキップ "
                f"({os.path.basename(path)})"
            )
            return
        try:
            flow = self._runner.load_flow(os.path.join(FLOWS_DIR, cur))
            self._update_schedule_list(flow.schedule)
            if self._runner.is_running:
                self._append_log(
                    f"フロー反映 (実行中: 次のチェックから新定義): {flow.name}"
                )
            else:
                self._append_log(
                    f"フロー反映 (待機中: 次回開始から新定義): {flow.name}"
                )
        except Exception as e:
            self._append_log(f"フロー反映失敗: {e}")

    def _on_flow_editor_closed(self, win) -> None:
        if hasattr(self, "_flow_editors") and win in self._flow_editors:
            self._flow_editors.remove(win)

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
            counts_state = load_watcher_counts()
            counts = counts_state.get("counts", {})
            last_fired = counts_state.get("last_fired", {})
            self._lbl_watcher_today.setText(f"本日 ({counts_state.get('date','')}) の発火")
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
                cnt = int(counts.get(w.id, 0))
                tlast = last_fired.get(w.id, "")
                label = self._fmt_watcher_label(w.title or fname, w.condition.type, cnt, tlast)
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, path)
                item.setData(Qt.UserRole + 1, w.id)
                item.setData(Qt.UserRole + 2, w.title or fname)
                item.setData(Qt.UserRole + 3, w.condition.type)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if w.enabled else Qt.Unchecked)
                self._list_watchers.addItem(item)
        finally:
            self._list_watchers.blockSignals(False)

    @staticmethod
    def _fmt_watcher_label(title: str, type_: str, count: int, last_time: str) -> str:
        if count > 0:
            tail = f"  本日 {count}回"
            if last_time:
                # HH:MM:SS → HH:MM に切り詰めて表示幅を抑える
                tail += f" / 最終 {last_time[:5]}"
        else:
            tail = "  本日 0回"
        return f"{title}  [{type_}]{tail}"

    def _on_watcher_fired_visual(
        self, watcher_id: str, title: str, count: int, fired_at: str,
    ) -> None:
        """ウォッチャー発火時に「見張り」タブのリスト行を更新 + 黄色ハイライト。"""
        if not watcher_id:
            return
        target_item: QListWidgetItem | None = None
        for i in range(self._list_watchers.count()):
            it = self._list_watchers.item(i)
            if it.data(Qt.UserRole + 1) == watcher_id:
                target_item = it
                break
        if target_item is None:
            # 新規ウォッチャーや一覧未反映の場合は次回 reload で拾われる
            return
        type_ = target_item.data(Qt.UserRole + 3) or ""
        target_item.setText(self._fmt_watcher_label(title, type_, count, fired_at))
        target_item.setBackground(QBrush(QColor("#fff176")))   # 黄
        # 3 秒後にハイライト解除
        QTimer.singleShot(3000, lambda it=target_item: self._clear_watcher_highlight(it))

    def _clear_watcher_highlight(self, item: QListWidgetItem) -> None:
        # 別 reload で item が破棄されている可能性に備える
        try:
            item.setBackground(QBrush())
        except RuntimeError:
            pass

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

    # ---- 録画タブ
    def _build_tab_record(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 8, 6, 6)
        lay.setSpacing(8)

        # コントロール
        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel("FPS:"))
        self._spin_rec_fps = QDoubleSpinBox()
        self._spin_rec_fps.setRange(0.5, 10.0)
        self._spin_rec_fps.setSingleStep(0.5)
        self._spin_rec_fps.setDecimals(1)
        self._spin_rec_fps.setValue(2.0)
        self._spin_rec_fps.setFixedWidth(70)
        self._spin_rec_fps.setToolTip(
            "推奨 2 fps（一晩でも数百MB級）。動きを細かく追いたい場合は上げる"
        )
        self._spin_rec_fps.valueChanged.connect(
            lambda v: self._runner.set_recorder(self._recorder, fps=v)
        )
        ctrl_row.addWidget(self._spin_rec_fps)
        ctrl_row.addStretch(1)
        self._btn_rec = QPushButton("⏺ 録画開始")
        self._btn_rec.clicked.connect(self._toggle_record)
        ctrl_row.addWidget(self._btn_rec)
        lay.addLayout(ctrl_row)

        self._lbl_rec_status = QLabel("待機中")
        self._lbl_rec_status.setStyleSheet("color:#555; font-size:12px;")
        self._lbl_rec_status.setWordWrap(True)
        lay.addWidget(self._lbl_rec_status)

        lay.addWidget(QLabel("録画ファイル:"))
        self._list_rec = QListWidget()
        self._list_rec.setStyleSheet("font-size:11px;")
        lay.addWidget(self._list_rec, 1)

        btn_row = QHBoxLayout()
        btn_open = QPushButton("フォルダを開く")
        btn_open.clicked.connect(self._open_recordings_dir)
        btn_refresh = QPushButton("一覧更新")
        btn_refresh.clicked.connect(self._reload_recordings)
        btn_row.addWidget(btn_open)
        btn_row.addWidget(btn_refresh)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        hint = QLabel(
            "フェーズ1: 録画のみ。録画ファイルからフレーム切り出して"
            "ウォッチャー/シーンに使う UI は次フェーズで実装予定。"
            "1 時間ごとに自動分割されます。"
        )
        hint.setStyleSheet("color:#666; font-size:11px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        # 録画ワーカー本体
        self._recorder = WindowRecorder()
        self._recorder.started.connect(self._on_record_started)
        self._recorder.stopped.connect(self._on_record_stopped)
        self._recorder.file_rotated.connect(self._on_record_rotated)
        self._recorder.stats_updated.connect(self._on_record_stats)
        self._recorder.error_occurred.connect(self._on_record_error)
        # ウォッチャー発火→ハンドラー完了の自動録画にも同じ recorder を流用
        self._runner.set_recorder(self._recorder, fps=float(self._spin_rec_fps.value()))

        self._reload_recordings()
        return w

    def _toggle_record(self) -> None:
        if self._recorder.is_recording:
            self._recorder.stop()
            return
        title = self._settings.get("window_title", "")
        hwnd = find_hwnd_by_title(title) if title else None
        if not hwnd:
            self._append_log(f"⚠ 録画: ウィンドウが見つかりません: {title!r}")
            return
        self._recorder.start(hwnd, fps=float(self._spin_rec_fps.value()))

    def _on_record_started(self, path: str) -> None:
        self._btn_rec.setText("⏹ 停止")
        self._btn_rec.setStyleSheet(
            "QPushButton{background:#c62828;color:white;font-weight:bold;}"
        )
        self._lbl_rec_status.setText(f"録画中… {os.path.basename(path)}")
        self._append_log(f"録画開始: {path}")
        self._reload_recordings()

    def _on_record_stopped(self) -> None:
        self._btn_rec.setText("⏺ 録画開始")
        self._btn_rec.setStyleSheet("")
        self._lbl_rec_status.setText("待機中")
        self._append_log("録画停止")
        self._reload_recordings()

    def _on_record_rotated(self, path: str) -> None:
        self._append_log(f"録画分割 → {os.path.basename(path)}")
        self._reload_recordings()

    def _on_record_stats(self, frames: int, elapsed: float) -> None:
        cur = self._recorder.current_path
        name = os.path.basename(cur) if cur else "(?)"
        size_mb = 0.0
        if cur and os.path.exists(cur):
            try:
                size_mb = os.path.getsize(cur) / (1024 * 1024)
            except OSError:
                size_mb = 0.0
        hh = int(elapsed // 3600)
        mm = int((elapsed % 3600) // 60)
        ss = int(elapsed % 60)
        self._lbl_rec_status.setText(
            f"録画中… {name}   {frames} フレーム  "
            f"{hh:02d}:{mm:02d}:{ss:02d}  {size_mb:.1f} MB"
        )

    def _on_record_error(self, msg: str) -> None:
        self._append_log(f"⚠ {msg}")
        self._lbl_rec_status.setText(f"⚠ {msg}")

    def _reload_recordings(self) -> None:
        self._list_rec.clear()
        if not os.path.isdir(RECORDINGS_DIR):
            return
        for fname in sorted(os.listdir(RECORDINGS_DIR), reverse=True):
            if not fname.lower().endswith(".mp4"):
                continue
            path = os.path.join(RECORDINGS_DIR, fname)
            try:
                size_mb = os.path.getsize(path) / (1024 * 1024)
                self._list_rec.addItem(f"{fname}   {size_mb:.1f} MB")
            except OSError:
                self._list_rec.addItem(fname)

    def _open_recordings_dir(self) -> None:
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        abspath = os.path.abspath(RECORDINGS_DIR)
        try:
            os.startfile(abspath)  # noqa: S606 — Windows のみ
        except Exception as e:
            self._append_log(f"⚠ フォルダオープン失敗: {e}")

    # ---- 経験値タブ: 単体 exp_meter アプリの主要機能をフロー UI 側に組込
    def _build_tab_exp_meter(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 8, 6, 6)
        lay.setSpacing(8)

        m = self._exp_meter
        # 対象ウィンドウはメインの window_title と同期させる（毎回 start 時にも再代入）
        m.window_title = (self._settings.get("window_title") or m.window_title or "")

        # 設定
        grp_cfg = QGroupBox("設定")
        cfg_lay = QFormLayout(grp_cfg)

        reg_row = QHBoxLayout()
        self._lbl_exp_region = QLabel("")
        self._btn_exp_region = QPushButton("変更…")
        self._btn_exp_region.clicked.connect(self._exp_pick_region)
        reg_row.addWidget(self._lbl_exp_region, 1)
        reg_row.addWidget(self._btn_exp_region)
        cfg_lay.addRow("計測領域:", reg_row)

        hint_row = QHBoxLayout()
        self._exp_rb1 = QRadioButton("1桁 (0〜9%台)")
        self._exp_rb2 = QRadioButton("2桁 (10〜99%台)")
        (self._exp_rb2 if m.digit_hint == 2 else self._exp_rb1).setChecked(True)
        self._exp_rb1.toggled.connect(self._exp_on_hint_changed)
        hint_row.addWidget(self._exp_rb1)
        hint_row.addWidget(self._exp_rb2)
        hint_row.addStretch(1)
        cfg_lay.addRow("桁数ヒント:", hint_row)

        self._exp_spin_interval = QSpinBox()
        self._exp_spin_interval.setRange(10, 600)
        self._exp_spin_interval.setSingleStep(5)
        self._exp_spin_interval.setSuffix(" 秒")
        self._exp_spin_interval.setValue(m.interval_sec)
        self._exp_spin_interval.valueChanged.connect(self._exp_on_interval_changed)
        cfg_lay.addRow("計測間隔:", self._exp_spin_interval)

        lay.addWidget(grp_cfg)

        # 計測状況
        grp_stat = QGroupBox("📊 計測状況")
        stat_lay = QVBoxLayout(grp_stat)
        self._exp_lbl_current = QLabel("現在値:  —")
        self._exp_lbl_cspd    = QLabel("現在速度:  —")
        self._exp_lbl_aspd    = QLabel("平均速度:  —")
        self._exp_lbl_eta     = QLabel("LvUP予測:  —")
        self._exp_lbl_meta    = QLabel("")
        self._exp_lbl_meta.setStyleSheet("color:#777; font-size:10px;")
        for lbl in (
            self._exp_lbl_current, self._exp_lbl_cspd,
            self._exp_lbl_aspd, self._exp_lbl_eta, self._exp_lbl_meta,
        ):
            stat_lay.addWidget(lbl)
        lay.addWidget(grp_stat)

        # コントロール
        ctrl = QHBoxLayout()
        self._exp_btn_start = QPushButton("▶ 計測開始")
        self._exp_btn_start.clicked.connect(self._exp_toggle)
        self._exp_btn_reset = QPushButton("🔄 リセット")
        self._exp_btn_reset.clicked.connect(self._exp_reset)
        self._exp_btn_overlay = QPushButton("🗕 ゲーム画面に重ねる")
        self._exp_btn_overlay.clicked.connect(self._exp_show_overlay)
        ctrl.addWidget(self._exp_btn_start)
        ctrl.addWidget(self._exp_btn_reset)
        ctrl.addWidget(self._exp_btn_overlay)
        ctrl.addStretch(1)
        lay.addLayout(ctrl)

        self._exp_lbl_status = QLabel("")
        self._exp_lbl_status.setStyleSheet("color:#666; font-size:11px;")
        self._exp_lbl_status.setWordWrap(True)
        lay.addWidget(self._exp_lbl_status)

        hint = QLabel(
            "OCR は「⚙ 設定」内のメイン Tesseract 設定を共用します。"
            "対象ウィンドウもメイン設定の値を使用。"
            "オーバーレイ位置は overlay_pos に保存されます。"
        )
        hint.setStyleSheet("color:#666; font-size:11px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        lay.addStretch(1)

        m.updated.connect(self._exp_refresh)
        m.status_changed.connect(self._exp_on_status)
        m.error.connect(self._exp_on_error)

        self._exp_refresh()
        return w

    def _exp_pick_region(self) -> None:
        from .region_picker import RegionPickerDialog
        title = (
            self._settings.get("window_title")
            or self._exp_meter.window_title
            or ""
        )
        if not title:
            QMessageBox.information(
                self, "情報",
                "先にメイン設定の対象ウィンドウを指定してください",
            )
            return
        dlg = RegionPickerDialog(
            title, self._exp_meter.region_rel, self,
        )
        if dlg.exec():
            r = dlg.get_rel()
            if r:
                self._exp_meter.region_rel = r
                self._exp_meter.save()
                self._exp_refresh()

    def _exp_on_hint_changed(self, *_) -> None:
        self._exp_meter.digit_hint = 2 if self._exp_rb2.isChecked() else 1
        self._exp_meter.save()

    def _exp_on_interval_changed(self, v: int) -> None:
        self._exp_meter.set_interval(int(v))

    def _exp_toggle(self) -> None:
        m = self._exp_meter
        # 開始時に window_title をメイン設定と同期
        m.window_title = (
            self._settings.get("window_title") or m.window_title or ""
        )
        if m.running:
            m.stop()
        else:
            m.start()
        self._exp_refresh()

    def _exp_reset(self) -> None:
        ret = QMessageBox.question(
            self, "確認", "サンプルと累積値をリセットしますか？",
        )
        if ret == QMessageBox.Yes:
            self._exp_meter.reset()
            self._exp_refresh()

    def _exp_show_overlay(self) -> None:
        m = self._exp_meter
        if not m.running:
            QMessageBox.information(
                self, "情報",
                "計測中にのみオーバーレイを表示できます",
            )
            return
        if self._exp_overlay is None:
            self._exp_overlay = ExpOverlayWindow(m)
            self._exp_overlay.request_setup.connect(self._exp_overlay_return)
            self._exp_overlay.request_toggle.connect(self._exp_toggle)
            self._exp_overlay.request_reset.connect(self._exp_reset)
            self._exp_overlay.request_quit.connect(self._exp_overlay_close)
        pos = self._settings.get("overlay_pos")
        if pos and isinstance(pos, list) and len(pos) == 2:
            self._exp_overlay.move(int(pos[0]), int(pos[1]))
        self._exp_overlay.show()

    def _exp_overlay_return(self) -> None:
        """オーバーレイの ⚙ ボタン: 経験値タブを最前面に呼び戻す。"""
        if self._exp_overlay is not None:
            p = self._exp_overlay.pos()
            self._settings["overlay_pos"] = [p.x(), p.y()]
            save_settings(self._settings)
            self._exp_overlay.hide()
        # 経験値タブをアクティブにしてからメインを前面化
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == "経験値":
                self._tabs.setCurrentIndex(i)
                break
        self.show()
        self.raise_()
        self.activateWindow()

    def _exp_overlay_close(self) -> None:
        if self._exp_overlay is not None:
            p = self._exp_overlay.pos()
            self._settings["overlay_pos"] = [p.x(), p.y()]
            save_settings(self._settings)
            self._exp_overlay.close()
            self._exp_overlay = None

    def _exp_on_status(self, s: str) -> None:
        self._exp_lbl_status.setText(s)
        self._exp_lbl_status.setStyleSheet("color:#666; font-size:11px;")

    def _exp_on_error(self, s: str) -> None:
        self._exp_lbl_status.setText(f"⚠ {s}")
        self._exp_lbl_status.setStyleSheet("color:#c62828; font-size:11px;")

    def _exp_refresh(self) -> None:
        m = self._exp_meter
        self._exp_btn_start.setText("■ 停止" if m.running else "▶ 計測開始")
        self._exp_btn_start.setStyleSheet(
            "QPushButton{background:#c62828;color:white;font-weight:bold;}"
            if m.running else ""
        )
        self._exp_btn_overlay.setEnabled(m.running)

        if m.region_rel:
            r = m.region_rel
            self._lbl_exp_region.setText(
                f"✓ 設定済み  (x={r[0]:.3f} y={r[1]:.3f} "
                f"w={r[2]:.3f} h={r[3]:.3f})"
            )
            self._lbl_exp_region.setStyleSheet("color:#2e7d32;")
        else:
            self._lbl_exp_region.setText("⚠ 未設定")
            self._lbl_exp_region.setStyleSheet("color:#c62828;")

        self._exp_lbl_current.setText(
            f"現在値:  {m.prev_raw:.4f}%" if m.prev_raw is not None
            else "現在値:  —"
        )
        cur = m.current_speed()
        avg = m.avg_speed()
        n = len(m.samples)
        if cur is not None:
            self._exp_lbl_cspd.setText(f"現在速度:  {cur:.1f} %/h")
        elif n > 0:
            self._exp_lbl_cspd.setText(
                f"現在速度:  — (あと{max(0, CURRENT_WINDOW - n)}サンプル)"
            )
        else:
            self._exp_lbl_cspd.setText("現在速度:  —")
        self._exp_lbl_aspd.setText(
            f"平均速度:  {avg:.1f} %/h" if avg is not None
            else "平均速度:  —"
        )

        eta_cur, eta_avg = m.eta_to_levelup()
        now = datetime.now()
        parts: list[str] = []
        for e, lab in ((eta_cur, "現在"), (eta_avg, "平均")):
            if e is None:
                continue
            if e >= 60:
                h, mn = divmod(int(e), 60)
                eta_str = f"{h}h{mn:02d}m"
            else:
                eta_str = f"{int(e)}分"
            target = now + timedelta(minutes=int(e))
            if target.date() == now.date():
                abs_str = target.strftime("%H:%M")
            else:
                abs_str = target.strftime("%m/%d %H:%M")
            parts.append(f"{eta_str}（{lab} → {abs_str}）")
        self._exp_lbl_eta.setText(
            "LvUP予測:  " + "  /  ".join(parts) if parts else "LvUP予測:  —"
        )

        last = m.samples[-1][0].strftime("%H:%M") if m.samples else "—"
        self._exp_lbl_meta.setText(
            f"計測: {m.elapsed_str()}  {n}サンプル  最終: {last}"
        )

    # ---- 翻訳タブ: 領域の定期キャプチャ → Claude API 翻訳、ユーザー入力翻訳
    def _build_tab_translation(self) -> QWidget:
        from .translation import LANG_CODES, LANG_LABELS_JA
        outer = QWidget()
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(6, 8, 6, 6)
        outer_lay.setSpacing(4)

        # 監視領域
        rgn_grp = QGroupBox("監視領域 (チャット欄をスクショ → 翻訳)")
        rgn_lay = QVBoxLayout(rgn_grp)
        rgn_lay.setContentsMargins(8, 6, 8, 6)
        rgn_lay.setSpacing(4)

        self._lbl_tr_region = QLabel(self._fmt_tr_region())
        self._lbl_tr_region.setStyleSheet("color:#555; font-size:11px;")
        self._lbl_tr_region.setWordWrap(True)
        rgn_lay.addWidget(self._lbl_tr_region)

        rgn_btn_row = QHBoxLayout()
        btn_pick = QPushButton("📐 画面から領域選択")
        btn_pick.setToolTip("ゲームウィンドウのスクショからドラッグで領域を指定")
        btn_pick.clicked.connect(self._tr_pick_region)
        rgn_btn_row.addWidget(btn_pick)

        btn_clear_rgn = QPushButton("クリア")
        btn_clear_rgn.setToolTip("領域指定を消す")
        btn_clear_rgn.clicked.connect(self._tr_clear_region)
        rgn_btn_row.addWidget(btn_clear_rgn)
        rgn_btn_row.addStretch(1)
        rgn_lay.addLayout(rgn_btn_row)

        # 間隔 + 開始/停止
        ctl_row = QHBoxLayout()
        ctl_row.addWidget(QLabel("間隔:"))
        self._spin_tr_interval = QDoubleSpinBox()
        self._spin_tr_interval.setRange(1.0, 600.0)
        self._spin_tr_interval.setSingleStep(1.0)
        self._spin_tr_interval.setDecimals(1)
        self._spin_tr_interval.setSuffix(" 秒")
        self._spin_tr_interval.setFixedWidth(96)
        self._spin_tr_interval.setValue(
            float(self._settings.get("translation_interval_s", 5.0))
        )
        ctl_row.addWidget(self._spin_tr_interval)
        ctl_row.addStretch(1)
        self._btn_tr_toggle = QPushButton("▶ 開始")
        self._btn_tr_toggle.setFixedWidth(80)
        self._btn_tr_toggle.setStyleSheet(
            "QPushButton{background:#2e7d32;color:white;font-weight:bold;}"
        )
        self._btn_tr_toggle.clicked.connect(self._tr_toggle_loop)
        ctl_row.addWidget(self._btn_tr_toggle)
        rgn_lay.addLayout(ctl_row)

        outer_lay.addWidget(rgn_grp)

        # スプリッタ: 上=チャット翻訳ログ / 下=ユーザー入力翻訳
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)

        # 上: チャット翻訳ログ
        chat_w = QWidget()
        chat_lay = QVBoxLayout(chat_w)
        chat_lay.setContentsMargins(0, 0, 0, 0)
        chat_lay.setSpacing(2)
        chat_lay.addWidget(QLabel("チャット翻訳ログ:"))
        self._tr_chat_log = QPlainTextEdit()
        self._tr_chat_log.setReadOnly(True)
        self._tr_chat_log.setStyleSheet(
            "font-family: Consolas, 'Yu Gothic UI', sans-serif; font-size: 12px;"
        )
        chat_lay.addWidget(self._tr_chat_log, 1)
        chat_btns = QHBoxLayout()
        chat_btns.addStretch(1)
        btn_chat_clear = QPushButton("クリア")
        btn_chat_clear.clicked.connect(self._tr_chat_log.clear)
        chat_btns.addWidget(btn_chat_clear)
        chat_lay.addLayout(chat_btns)
        splitter.addWidget(chat_w)

        # 下: ユーザー入力翻訳
        usr_w = QWidget()
        usr_lay = QVBoxLayout(usr_w)
        usr_lay.setContentsMargins(0, 0, 0, 0)
        usr_lay.setSpacing(2)
        usr_lay.addWidget(QLabel("自分の発言を翻訳:"))
        self._tr_user_input = QPlainTextEdit()
        self._tr_user_input.setPlaceholderText(
            "翻訳したいテキストを入力 (Ctrl+Enter で翻訳実行)"
        )
        self._tr_user_input.setFixedHeight(80)
        self._tr_user_input.setStyleSheet(
            "font-family: 'Yu Gothic UI', sans-serif; font-size: 12px;"
        )
        # Ctrl+Enter で翻訳
        self._tr_user_input.installEventFilter(self)
        usr_lay.addWidget(self._tr_user_input)

        tgt_row = QHBoxLayout()
        tgt_row.addWidget(QLabel("対象:"))
        cur_targets = set(self._settings.get("translation_user_targets") or [])
        self._tr_target_checks: dict[str, QCheckBox] = {}
        for code in LANG_CODES:
            cb = QCheckBox(f"{LANG_LABELS_JA[code]}")
            cb.setChecked(code in cur_targets)
            cb.stateChanged.connect(self._tr_save_targets)
            tgt_row.addWidget(cb)
            self._tr_target_checks[code] = cb
        tgt_row.addStretch(1)
        usr_lay.addLayout(tgt_row)

        run_row = QHBoxLayout()
        run_row.addStretch(1)
        self._btn_tr_user_translate = QPushButton("翻訳")
        self._btn_tr_user_translate.setFixedWidth(80)
        self._btn_tr_user_translate.clicked.connect(self._tr_translate_user)
        run_row.addWidget(self._btn_tr_user_translate)
        usr_lay.addLayout(run_row)

        usr_lay.addWidget(QLabel("翻訳結果:"))
        self._tr_user_output = QPlainTextEdit()
        self._tr_user_output.setReadOnly(True)
        self._tr_user_output.setStyleSheet(
            "font-family: Consolas, 'Yu Gothic UI', sans-serif; font-size: 12px;"
        )
        usr_lay.addWidget(self._tr_user_output, 1)
        splitter.addWidget(usr_w)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([320, 240])
        outer_lay.addWidget(splitter, 1)

        # 内部状態
        self._tr_thread = None
        self._tr_stop = threading.Event()
        # 別スレッド → メインへの安全なテキスト追記 / ボタン状態
        self._tr_chat_signal.connect(self._tr_chat_append)
        self._tr_user_signal.connect(self._tr_user_append)
        self._tr_user_busy_signal.connect(
            lambda busy: self._btn_tr_user_translate.setEnabled(not busy)
        )
        return outer

    # ---- 翻訳タブのヘルパー --------------------------------------------
    def _fmt_tr_region(self) -> str:
        r = self._settings.get("translation_region")
        if not r or len(r) != 4:
            return "領域: （未設定）— 「画面から領域選択」で指定してください"
        return (
            f"領域(比率): x={r[0]:.4f} y={r[1]:.4f} w={r[2]:.4f} h={r[3]:.4f}"
        )

    def _tr_pick_region(self) -> None:
        from .region_picker import RegionPickerDialog
        title = self._settings.get("window_title", "")
        if not title:
            QMessageBox.warning(
                self, "ウィンドウ未指定",
                "先にヘッダーで対象ゲームウィンドウを選択してください。",
            )
            return
        cur = self._settings.get("translation_region")
        dlg = RegionPickerDialog(
            title,
            current_rel=cur if cur and len(cur) == 4 else None,
            parent=self,
            dialog_title="チャット監視領域を選択",
            hint_text=(
                "ゲームウィンドウのスクショからチャット欄をドラッグで囲んでください。\n"
                "ホイール=ズーム / 右ドラッグ=パン / 領域はウィンドウサイズ比率で保存します。"
            ),
        )
        if dlg.exec():
            rel = dlg.get_rel()
            if rel and len(rel) == 4:
                self._settings["translation_region"] = rel
                save_settings(self._settings)
                self._lbl_tr_region.setText(self._fmt_tr_region())

    def _tr_clear_region(self) -> None:
        self._settings["translation_region"] = None
        save_settings(self._settings)
        self._lbl_tr_region.setText(self._fmt_tr_region())

    def _tr_save_targets(self) -> None:
        targets = [
            code for code, cb in self._tr_target_checks.items() if cb.isChecked()
        ]
        self._settings["translation_user_targets"] = targets
        save_settings(self._settings)

    def _tr_toggle_loop(self) -> None:
        if self._tr_thread and self._tr_thread.is_alive():
            self._tr_stop.set()
            self._btn_tr_toggle.setText("▶ 開始")
            self._btn_tr_toggle.setStyleSheet(
                "QPushButton{background:#2e7d32;color:white;font-weight:bold;}"
            )
            self._tr_chat_signal.emit("--- 翻訳ループ停止 ---")
            return
        # 起動前チェック
        key = (self._settings.get("translation_api_key") or "").strip()
        if not key:
            QMessageBox.warning(
                self, "API キー未設定",
                "設定で Claude API キーを入力してください。",
            )
            return
        region = self._settings.get("translation_region")
        if not region or len(region) != 4:
            QMessageBox.warning(
                self, "領域未指定",
                "監視領域を選択してください（「画面から領域選択」ボタン）。",
            )
            return
        title = self._settings.get("window_title", "")
        if not title:
            QMessageBox.warning(
                self, "ウィンドウ未指定",
                "ヘッダーで対象ゲームウィンドウを選択してください。",
            )
            return
        # 間隔を保存
        self._settings["translation_interval_s"] = float(self._spin_tr_interval.value())
        save_settings(self._settings)

        self._tr_stop.clear()
        self._tr_thread = threading.Thread(
            target=self._tr_loop_worker, daemon=True,
        )
        self._tr_thread.start()
        self._btn_tr_toggle.setText("■ 停止")
        self._btn_tr_toggle.setStyleSheet(
            "QPushButton{background:#c62828;color:white;font-weight:bold;}"
        )
        self._tr_chat_signal.emit(
            f"--- 翻訳ループ開始 (間隔 {self._spin_tr_interval.value():.1f}s) ---"
        )

    def _tr_loop_worker(self) -> None:
        """別スレッド: 一定間隔で監視領域をキャプチャ → Claude API → ログ追記。"""
        from .capture import capture_window
        from .translation import TranslationClient, LANG_LABELS_JA
        import cv2

        key = (self._settings.get("translation_api_key") or "").strip()
        base = (self._settings.get("translation_base_lang") or "ja").lower()
        try:
            client = TranslationClient(api_key=key)
        except ImportError as e:
            self._tr_chat_signal.emit(f"⚠ {e}")
            return
        title = self._settings.get("window_title", "")
        interval = max(1.0, float(self._spin_tr_interval.value()))

        while not self._tr_stop.is_set():
            hwnd = find_hwnd_by_title(title) if title else None
            if not hwnd:
                self._tr_chat_signal.emit("⚠ 対象ウィンドウが見つかりません")
                # 短く待って再試行
                self._tr_sleep(min(interval, 3.0))
                continue
            img = capture_window(hwnd)
            if img is None:
                self._tr_sleep(interval)
                continue
            region = self._settings.get("translation_region")
            if not region or len(region) != 4:
                self._tr_chat_signal.emit("⚠ 領域未指定（停止します）")
                break
            ih, iw = img.shape[:2]
            rx, ry, rw, rh = region
            x0 = max(0, int(rx * iw))
            y0 = max(0, int(ry * ih))
            x1 = min(iw, int((rx + rw) * iw))
            y1 = min(ih, int((ry + rh) * ih))
            if x1 <= x0 or y1 <= y0:
                self._tr_chat_signal.emit("⚠ 領域サイズが不正")
                self._tr_sleep(interval)
                continue
            crop = img[y0:y1, x0:x1]
            ok, buf = cv2.imencode(".png", crop)
            if not ok:
                self._tr_sleep(interval)
                continue
            try:
                msgs = client.translate_image(bytes(buf), base)
            except Exception as e:
                self._tr_chat_signal.emit(f"⚠ API 例外: {e}")
                self._tr_sleep(interval)
                continue
            if msgs:
                ts = datetime.now().strftime("%H:%M:%S")
                lines: list[str] = []
                for m in msgs:
                    lang = m.get("lang", "?")
                    orig = m.get("original", "")
                    trans = m.get("translated")
                    lang_label = LANG_LABELS_JA.get(lang, lang)
                    lines.append(f"[{ts}] [{lang_label}] {orig}")
                    if trans:
                        lines.append(f"    → [{LANG_LABELS_JA.get(base, base)}] {trans}")
                self._tr_chat_signal.emit("\n".join(lines))
            self._tr_sleep(interval)

        self._tr_chat_signal.emit("--- 翻訳ループ終了 ---")

    def _tr_sleep(self, secs: float) -> None:
        """停止フラグを 100ms 毎に確認しつつスリープ。"""
        end = time.monotonic() + max(0.0, secs)
        while time.monotonic() < end:
            if self._tr_stop.is_set():
                return
            time.sleep(0.1)

    def _tr_chat_append(self, text: str) -> None:
        self._tr_chat_log.appendPlainText(text)
        sb = self._tr_chat_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _tr_user_append(self, text: str) -> None:
        self._tr_user_output.appendPlainText(text)
        sb = self._tr_user_output.verticalScrollBar()
        sb.setValue(sb.maximum())

    def eventFilter(self, obj, event):  # noqa: N802
        """Ctrl+Enter で翻訳タブのユーザー入力を翻訳実行。"""
        if (hasattr(self, "_tr_user_input")
                and obj is self._tr_user_input
                and event.type() == QEvent.KeyPress):
            key = event.key()
            mods = event.modifiers()
            if (key in (Qt.Key_Return, Qt.Key_Enter)
                    and (mods & Qt.ControlModifier)):
                self._tr_translate_user()
                return True
        return super().eventFilter(obj, event)

    def _tr_translate_user(self) -> None:
        text = self._tr_user_input.toPlainText().strip()
        if not text:
            return
        targets = [
            code for code, cb in self._tr_target_checks.items() if cb.isChecked()
        ]
        if not targets:
            QMessageBox.warning(
                self, "対象言語未指定",
                "翻訳先の言語を 1 つ以上チェックしてください。",
            )
            return
        key = (self._settings.get("translation_api_key") or "").strip()
        if not key:
            QMessageBox.warning(
                self, "API キー未設定",
                "設定で Claude API キーを入力してください。",
            )
            return
        # 別スレッドで API 呼び出し（UI ブロックを避ける）
        self._tr_user_busy_signal.emit(True)
        self._tr_user_signal.emit(f"--- 翻訳開始: {text[:40]}{'…' if len(text)>40 else ''} ---")

        def _worker() -> None:
            from .translation import TranslationClient, LANG_LABELS_JA
            try:
                client = TranslationClient(api_key=key)
                results = client.translate_text(text, targets)
            except Exception as e:
                self._tr_user_signal.emit(f"⚠ 例外: {e}")
                self._tr_user_signal.emit("--- 完了 ---")
                self._tr_user_busy_signal.emit(False)
                return
            ts = datetime.now().strftime("%H:%M:%S")
            lines = [f"[{ts}] 原文: {text}"]
            for code in targets:
                if code in results:
                    lines.append(f"  [{LANG_LABELS_JA.get(code, code)}] {results[code]}")
                else:
                    lines.append(f"  [{LANG_LABELS_JA.get(code, code)}] (取得失敗)")
            self._tr_user_signal.emit("\n".join(lines))
            self._tr_user_signal.emit("--- 完了 ---")
            self._tr_user_busy_signal.emit(False)

        threading.Thread(target=_worker, daemon=True).start()

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
        self._runner.watcher_fired_visual.connect(self._on_watcher_fired_visual)
        self._runner.foreground_confirm_request.connect(
            self._on_foreground_confirm_request
        )

        # フロー実行中オーバーレイ（半透明・最前面）
        self._flow_overlay = FlowOverlay()
        self._flow_overlay.request_stop_flow.connect(self._on_overlay_stop_flow)
        self._flow_overlay.request_hide.connect(self._on_overlay_hide)
        self._flow_overlay.moved.connect(self._on_overlay_moved)
        # ランナーシグナルをオーバーレイへも転送
        self._runner.scene_started.connect(self._flow_overlay.update_scene)
        self._runner.step_updated.connect(self._flow_overlay.update_step)
        self._runner.next_schedule_changed.connect(
            self._flow_overlay.update_next_schedule
        )
        self._runner.state_changed.connect(self._flow_overlay.update_state)
        self._runner.watcher_fired_visual.connect(
            self._flow_overlay.show_watcher_fired
        )
        # 起動時の位置復元
        pos = self._settings.get("flow_overlay_pos")
        if pos and isinstance(pos, (list, tuple)) and len(pos) == 2:
            try:
                self._flow_overlay.move(int(pos[0]), int(pos[1]))
            except (TypeError, ValueError):
                pass
        self._test_log_signal.connect(self._test_log_append)

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

        # フローを必ずロードする。
        # _load_flows_list() で blockSignals 中に項目を addItem しているため、内部的に
        # currentIndex が 0 へ移った状態で signal が抑止される。直後の setCurrentIndex(0)
        # は「同じ index」扱いで currentIndexChanged が発火せず、_on_flow_selected が
        # 呼ばれない（結果としてフロー未ロードで実行タブのテーブルが空になる）。
        # 明示的に現在選択中のインデックスでロードを走らせる。
        idx = self._combo_flow.currentIndex()
        if idx >= 0 and self._combo_flow.itemData(idx):
            self._on_flow_selected(idx)

        # 通知設定をランナーへ
        self._runner.set_notify_webhook(
            self._settings.get("google_chat_webhook", "") or ""
        )
        self._runner.set_foreground_check_interval_min(
            float(self._settings.get("foreground_check_interval_min", 5.0))
        )

    # ---------------------------------------------------------------- 設定ダイアログ
    def _open_settings(self) -> None:
        dlg = _SettingsDialog(self._settings, self)
        if dlg.exec():
            self._settings.update(dlg.result_settings())
            save_settings(self._settings)
            # 即時反映
            self._runner.set_notify_webhook(
                self._settings.get("google_chat_webhook", "") or ""
            )
            self._runner.set_foreground_check_interval_min(
                float(self._settings.get("foreground_check_interval_min", 5.0))
            )
            # オーバーレイ表示の即時反映: 実行中なら ON/OFF を反映する
            enabled = bool(self._settings.get("flow_overlay_enabled", True))
            if enabled and self._runner.is_running:
                self._flow_overlay.show()
            elif not enabled:
                self._flow_overlay.hide()
            self._append_log("設定を保存しました")

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
        """フロー読込時のフック: 現在表示中の曜日でフィルタして表示する。"""
        self._refresh_day_list()

    # ---- 曜日ページャー
    def _show_prev_day(self) -> None:
        self._displayed_weekday = (self._displayed_weekday - 1) % 7
        self._update_day_label()
        self._refresh_day_list()

    def _show_next_day(self) -> None:
        self._displayed_weekday = (self._displayed_weekday + 1) % 7
        self._update_day_label()
        self._refresh_day_list()

    def _show_today(self) -> None:
        self._displayed_weekday = datetime.now().weekday()
        self._update_day_label()
        self._refresh_day_list()

    def _update_day_label(self) -> None:
        wd = self._displayed_weekday
        today_wd = datetime.now().weekday()
        offset = (wd - today_wd) % 7
        name = DAY_NAMES[wd]
        if offset == 0:
            self._lbl_day.setText(f"今日 — {name}曜日")
            self._lbl_day.setStyleSheet("font-weight:bold; color:#1565c0;")
        else:
            self._lbl_day.setText(f"{name}曜日（+{offset}日）")
            self._lbl_day.setStyleSheet("font-weight:bold; color:#555;")

    def _refresh_day_list(self) -> None:
        """現在の `_displayed_weekday` に該当するエントリを 24時間 × 1列のテーブルに配置。

        - 行: 0〜23 時 (1 時間刻み)
        - 各エントリは「該当時間の行」に「HH:MM シーン名」として配置
        - 同じ時間帯に複数エントリがあれば改行で連結
        - seq（続けて実行）エントリは親エントリと同じセルに "→ シーン" として追記
        """
        self._sched_table.clearContents()
        # 前回拡張された行高さをリセット（今回コンテンツが減っても痕跡が残らないように）
        for r in range(self._sched_table.HOURS):
            self._sched_table.setRowHeight(r, self._sched_table.ROW_H)
        flow = self._runner._flow
        if not flow:
            return
        wd = self._displayed_weekday

        # schedule 順序を保ったまま (parent, [seqs]) を組み立て
        units: list[tuple[object, list[object]]] = []
        cur_parent = None
        cur_seqs: list = []
        for entry in flow.schedule:
            if not entry.enabled:
                continue
            if entry.seq:
                if cur_parent is not None:
                    cur_seqs.append(entry)
                continue
            if cur_parent is not None:
                units.append((cur_parent, cur_seqs))
            cur_parent = entry
            cur_seqs = []
        if cur_parent is not None:
            units.append((cur_parent, cur_seqs))

        def _matches(e) -> bool:
            if e.repeat == "daily":
                return True
            if e.repeat == "weekly":
                return (not e.days) or (wd in e.days)
            if e.repeat == "once":
                try:
                    d = datetime.strptime(e.date, "%Y-%m-%d").date()
                    return d.weekday() == wd
                except (ValueError, TypeError):
                    return False
            return False

        units = [(p, s) for p, s in units if _matches(p)]
        units.sort(key=lambda u: u[0].time)

        # 同一時間帯に複数ユニットが落ちる場合は改行で連結
        for parent, seqs in units:
            try:
                h = int(parent.time.split(":")[0])
            except (ValueError, AttributeError):
                continue
            if not (0 <= h < self._sched_table.HOURS):
                continue

            scenes = entry_scenes(parent)
            name = (
                os.path.splitext(scenes[0])[0]
                if scenes else (parent.target or "(未設定)")
            )
            lines = [f"{parent.time} {name}"]
            for seq_entry in seqs:
                ss = entry_scenes(seq_entry)
                sname = (
                    os.path.splitext(ss[0])[0]
                    if ss else (seq_entry.target or "(未設定)")
                )
                lines.append(f"   → {sname}")
            text = "\n".join(lines)

            # このユニット (parent + seqs) が実行する全シーン
            chain_scenes = list(entry_scenes(parent))
            for sq in seqs:
                chain_scenes.extend(entry_scenes(sq))

            existing = self._sched_table.item(h, 0)
            if existing and existing.text():
                text = existing.text() + "\n" + text
                # 既存のチェーンに後続ユニットを連結（右クリック実行用）
                prev_chains = existing.data(Qt.UserRole) or []
                merged_chains = list(prev_chains) + [chain_scenes]
            else:
                merged_chains = [chain_scenes]
            item = QTableWidgetItem(text)
            # 繰り返しタイプで色分け（全体フローと同じ配色）
            if parent.repeat == "daily":
                item.setForeground(QBrush(QColor("#1565c0")))
            elif parent.repeat == "weekly":
                item.setForeground(QBrush(QColor("#2e7d32")))
            elif parent.repeat == "once":
                item.setForeground(QBrush(QColor("#ef6c00")))
            # 同一セル内の複数ユニットを区別するため、各ユニットのシーン列のリストを保持
            item.setData(Qt.UserRole, merged_chains)
            # 複数行が入る可能性があるので行高さを内容に応じて広げる
            self._sched_table.setItem(h, 0, item)
            need_h = (len(text.split("\n"))) * 18 + 8
            if need_h > self._sched_table.rowHeight(h):
                self._sched_table.setRowHeight(h, need_h)

        # 今日を表示中なら現在時刻の行が見える位置までスクロール
        if self._displayed_weekday == datetime.now().weekday():
            cur_row = datetime.now().hour
            item = self._sched_table.item(cur_row, 0)
            if item is None:
                # 空セルでもスクロール位置の指定にだけ使う
                placeholder = QTableWidgetItem("")
                self._sched_table.setItem(cur_row, 0, placeholder)
                item = placeholder
            self._sched_table.scrollToItem(
                item, QAbstractItemView.PositionAtCenter,
            )

    def _on_sched_table_context_menu(self, pos) -> None:
        """日次テーブルの右クリック → セルのエントリを単発実行。"""
        item = self._sched_table.itemAt(pos)
        if item is None:
            return
        chains = item.data(Qt.UserRole)
        if not chains:
            return
        menu = QMenu(self._sched_table)
        # chains は [[scene1, seq1, seq2], [scene3], ...] の構造
        for idx, chain in enumerate(chains):
            if not chain:
                continue
            head = os.path.splitext(os.path.basename(chain[0]))[0]
            if len(chain) > 1:
                label = f"▶ {head} → 続けて {len(chain)-1} 件 を実行"
            else:
                label = f"▶ {head} を実行"
            act = menu.addAction(label)
            act.triggered.connect(
                lambda _checked=False, c=chain: self._run_scene_chain_manual(c)
            )
        if menu.actions():
            menu.exec(self._sched_table.viewport().mapToGlobal(pos))

    def _on_sched_table_double_clicked(self, row: int, col: int) -> None:
        """セルをダブルクリック → 該当エントリの編集ダイアログを開く。

        row は時刻の「時 (HH)」部分を示す。表示中曜日 (self._displayed_weekday)
        とエントリの repeat / days / date を見て、該当する非 seq エントリを集める。
        編集ダイアログ (`_EntryDialog`) は QTimeEdit で時刻を 1 分単位で指定できる。
        """
        flow = self._runner._flow
        if flow is None:
            return
        cur_name = self._combo_flow.currentData()
        if not cur_name:
            return
        path = os.path.join(FLOWS_DIR, cur_name)
        if not os.path.exists(path):
            return

        today_wd = self._displayed_weekday
        candidates: list[ScheduleEntry] = []
        for entry in flow.schedule:
            if entry.seq:
                continue
            try:
                h = int(entry.time.split(":")[0])
            except (ValueError, AttributeError):
                continue
            if h != row:
                continue
            if entry.repeat == "weekly":
                if entry.days and today_wd not in entry.days:
                    continue
            elif entry.repeat == "once":
                try:
                    d = datetime.strptime(entry.date, "%Y-%m-%d").date()
                    if d.weekday() != today_wd:
                        continue
                except (ValueError, TypeError):
                    continue
            # daily は曜日制限なし
            candidates.append(entry)

        if not candidates:
            return

        # 同一時刻枠に複数エントリがあれば選択メニュー、単一なら直接編集
        target: ScheduleEntry | None = None
        if len(candidates) > 1:
            menu = QMenu(self._sched_table)
            mapping: dict[int, ScheduleEntry] = {}
            for ent in candidates:
                scenes = entry_scenes(ent)
                name = (
                    os.path.splitext(scenes[0])[0]
                    if scenes else (ent.target or "(未設定)")
                )
                act = menu.addAction(f"{ent.time} {name} を編集")
                mapping[id(act)] = ent
            chosen = menu.exec(QCursor.pos())
            if chosen is None:
                return
            target = mapping.get(id(chosen))
        else:
            target = candidates[0]

        if target is None:
            return
        self._edit_schedule_entry_inline(flow, path, target)

    def _edit_schedule_entry_inline(
        self, flow, flow_path: str, entry: ScheduleEntry,
    ) -> None:
        """実行タブから ScheduleEntry を編集 → 保存 → ランナーへ即時反映する。"""
        scenes_list = self._available_flow_scenes()
        dlg = _EntryDialog(entry, scenes_list, self)
        if not dlg.exec():
            return
        try:
            save_pc_flow(flow, flow_path)
        except Exception as e:
            QMessageBox.warning(self, "保存失敗", str(e))
            return
        # ランナーに最新フローを差し替え（実行中なら次のチェックから新定義で動く）
        try:
            new_flow = self._runner.load_flow(flow_path)
            self._update_schedule_list(new_flow.schedule)
        except Exception as e:
            self._append_log(f"フロー再ロード失敗: {e}")
            return
        scenes = entry_scenes(entry)
        sname = (
            os.path.splitext(scenes[0])[0]
            if scenes else (entry.target or "(未設定)")
        )
        self._append_log(f"✓ スケジュール更新: {entry.time} {sname}")

    def _available_flow_scenes(self) -> list[str]:
        """フロー編集ダイアログ用のシーン候補（flow_target=True のみ）。"""
        if not os.path.isdir(SCENES_DIR):
            return []
        out: list[str] = []
        for fname in sorted(os.listdir(SCENES_DIR)):
            if not fname.endswith(".json") or fname.startswith("_"):
                continue
            full = os.path.join(SCENES_DIR, fname)
            try:
                scene = load_pc_scene(full)
                if not scene.flow_target:
                    continue
            except Exception:
                # 読めなくても候補に出す（隠して存在を忘れるよりマシ）
                pass
            out.append(fname)
        return out

    def _run_scene_chain_manual(self, scenes: list[str]) -> None:
        """実行タブのセルから選ばれたシーン列を単発実行する。"""
        if not scenes:
            return
        if self._runner.is_busy:
            QMessageBox.information(
                self, "実行中",
                "スケジュール実行または単発実行が進行中です。先に停止してください。",
            )
            return
        if self._mouse is None:
            ans = QMessageBox.question(
                self, "Pico 未接続",
                "Pico マウス未接続です。tap/swipe 系のステップはスキップされます。\n"
                "続行しますか？",
            )
            if ans != QMessageBox.Yes:
                return
        self._runner.set_window_title(self._settings.get("window_title", ""))
        self._runner.set_mouse(self._mouse)
        if not self._runner.run_scenes_async(scenes):
            self._append_log("⚠ 単発実行を受け付けられませんでした")

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
        if self._runner.is_busy:
            # スケジュール実行・単発実行のいずれもこれで止まる
            self._runner.stop()
        else:
            if self._runner._flow is None:
                self._append_log("フローが選択されていません")
                return
            self._runner.start()

    def _on_state_changed(self, state: str) -> None:
        if state == "idle":
            self._btn_flow.setText("▶ 開始")
            self._btn_flow.setStyleSheet(
                "QPushButton{background:#2e7d32;color:white;font-weight:bold;}"
            )
            self._lbl_run_status.setText("待機中")
            self._flow_overlay.hide()
        elif state == "running":
            self._btn_flow.setText("■ 停止")
            self._btn_flow.setStyleSheet(
                "QPushButton{background:#c62828;color:white;font-weight:bold;}"
            )
            # 設定で有効になっていれば自動表示
            if self._settings.get("flow_overlay_enabled", True):
                self._flow_overlay.show()

    # ---- オーバーレイのイベント
    def _on_overlay_stop_flow(self) -> None:
        """オーバーレイの「⏸ 停止」 → フロー停止要求。"""
        if self._runner.is_running or self._runner.is_busy:
            self._runner.stop()
            self._append_log("オーバーレイからフロー停止要求")

    def _on_overlay_hide(self) -> None:
        """オーバーレイの「✕」 → 表示だけ消す（フローは止めない）。
        以後の自動表示も止めるよう設定もオフにする。
        """
        self._flow_overlay.hide()
        self._settings["flow_overlay_enabled"] = False
        save_settings(self._settings)
        self._append_log("オーバーレイを非表示にしました（設定で再表示可能）")

    def _on_overlay_moved(self, x: int, y: int) -> None:
        """ドラッグ終了位置を settings に保存。"""
        self._settings["flow_overlay_pos"] = [int(x), int(y)]
        save_settings(self._settings)

    def _on_scene_started(self, name: str, step: int, total: int) -> None:
        self._lbl_run_status.setText(f"実行中: {name}  ステップ {step}/{total}")

    def _on_step_updated(self, step: int, total: int) -> None:
        scene = self._runner.current_scene
        self._lbl_run_status.setText(f"実行中: {scene}  ステップ {step}/{total}")

    def _on_foreground_confirm_request(self, scene_name: str, done) -> None:
        """ランナーからの前面確認要求 → ダイアログを表示して結果をランナーへ返す。

        done は threading.Event。ダイアログ閉じ後に set() で待機解放する。
        """
        try:
            dlg = ForegroundConfirmDialog(scene_name, parent=self)
            dlg.exec()
            self._runner.set_foreground_choice(dlg.choice)
        finally:
            # 例外が発生してもランナー側を永遠に待たせない
            try:
                done.set()
            except Exception:
                pass

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
        try:
            ov = _ClickCaptureOverlay()
            ov.clicked.connect(self._on_capture_clicked)
            ov.finished.connect(self._on_capture_finished)
            self._capture_overlay = ov
            ov.show()
            ov.raise_()
            ov.activateWindow()
        except Exception as e:
            import traceback
            self._test_log_append(f"⚠ オーバーレイ起動失敗: {e}")
            traceback.print_exc()
            self._capture_overlay = None

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

    # ---------------- 3 点連続クリック検証（HID 直接 / 画面外パーク経由）
    def _collect_seq_points(self) -> list[tuple[str, int, int]] | None:
        """連続クリック検証セクション専用の 3 点入力欄から座標を回収。"""
        points: list[tuple[str, int, int]] = []
        for i, (xinp, yinp) in enumerate(self._seq_inputs, start=1):
            label = f"{i}点目"
            try:
                x = int(xinp.text())
                y = int(yinp.text())
            except ValueError:
                self._test_log_append(f"⚠ {label} の座標を整数で入力してください")
                return None
            points.append((label, x, y))
        return points

    def _start_capture_seq(self, idx: int) -> None:
        """連続クリック検証の `idx` 点目（1〜3）の座標を、次の 1 クリックで取得する。"""
        if not (1 <= idx <= len(self._seq_inputs)):
            return
        if self._capture_overlay is not None:
            # 連打されたら現行キャプチャを終了
            self._capture_overlay.close()
            return
        self._test_log_append(
            f"連続クリック {idx} 点目: 左クリックで取得（右クリック・ESC で中断）"
        )
        try:
            ov = _ClickCaptureOverlay()
            ov.clicked.connect(lambda x, y, i=idx: self._on_capture_seq_clicked(i, x, y))
            ov.finished.connect(self._on_capture_finished)
            self._capture_overlay = ov
            self._capture_count = 0
            ov.show()
            ov.raise_()
            ov.activateWindow()
        except Exception as e:
            import traceback
            self._test_log_append(f"⚠ オーバーレイ起動失敗: {e}")
            traceback.print_exc()
            self._capture_overlay = None

    def _on_capture_seq_clicked(self, idx: int, x: int, y: int) -> None:
        if not (1 <= idx <= len(self._seq_inputs)):
            return
        xinp, yinp = self._seq_inputs[idx - 1]
        xinp.setText(str(x))
        yinp.setText(str(y))
        self._capture_count += 1   # _on_capture_finished の「中断（取得なし）」誤判定を避ける
        self._test_log_append(f"連続クリック {idx} 点目を取得: ({x}, {y})")
        # 1 点取得したらオーバーレイを閉じる。
        # close() は finished シグナルを発しないので、明示的に finished を emit して
        # _on_capture_finished で self._capture_overlay = None になるようにする
        # （これをしないと次の「取得」ボタン押下時に「既に取得中」扱いになる）。
        ov = self._capture_overlay
        if ov is not None:
            ov.finished.emit()
            ov.close()

    def _test_seq_hid(self) -> None:
        """A: 毎回 click_at（HID 移動 → CLICK）で 3 点連続クリック。"""
        if not self._mouse:
            self._test_log_append("⚠ Pico 未接続")
            return
        points = self._collect_seq_points()
        if points is None:
            return
        wait_s = float(self._spin_seq_wait.value())
        self._test_log_append(
            f"--- [A] HID 直接で 3 点連続クリック (間隔 {wait_s}s) ---"
        )

        def _worker() -> None:
            for i, (label, x, y) in enumerate(points):
                if i > 0:
                    time.sleep(wait_s)
                self._test_log_signal.emit(
                    f"  [{i+1}/{len(points)}] {label} → click_at ({x},{y})"
                )
                try:
                    fx, fy = self._mouse.click_at(x, y, "L", hold_ms=50)
                    self._test_log_signal.emit(
                        f"    実位置 ({fx},{fy})  誤差 ({fx-x:+d},{fy-y:+d})"
                    )
                except Exception as e:
                    self._test_log_signal.emit(f"    ⚠ 例外: {e}")
            self._test_log_signal.emit("--- [A] 完了 ---")

        threading.Thread(target=_worker, daemon=True).start()

    def _test_seq_park(self) -> None:
        """B: 対象ウィンドウ外へ HID で逃がす → SetCursorPos → CLICK を 3 回。"""
        if not self._mouse:
            self._test_log_append("⚠ Pico 未接続")
            return
        title = self._settings.get("window_title", "")
        hwnd = find_hwnd_by_title(title) if title else None
        if not hwnd:
            self._test_log_append(f"⚠ 対象ウィンドウが見つかりません: {title!r}")
            return
        points = self._collect_seq_points()
        if points is None:
            return
        wait_s = float(self._spin_seq_wait.value())

        # 対象ウィンドウのクライアント領域の絶対矩形を取得し、その右上外側へ逃がす。
        # 右下だと Windows のタスクバーが反応してメニューが開く事があるため、
        # 上方向（ウィンドウのタイトルバーより上）に逃がす。
        try:
            from .capture import get_client_screen_rect
            left, top, w_, h_ = get_client_screen_rect(hwnd)
            park_x = min(left + w_ + 80, 32760)
            park_y = max(top - 80, 0)
        except Exception as e:
            self._test_log_append(f"⚠ パーク位置算出失敗: {e}")
            return
        self._test_log_append(
            f"--- [B] 画面外パーク({park_x},{park_y}) → SetCursorPos で "
            f"3 点連続クリック (間隔 {wait_s}s) ---"
        )

        def _worker() -> None:
            for i, (label, x, y) in enumerate(points):
                if i > 0:
                    time.sleep(wait_s)
                # 1) ウィンドウ外へ HID で逃がす
                try:
                    self._mouse.move_to(park_x, park_y, max_step=40, delay=0.005)
                    time.sleep(0.05)
                    px, py = self._mouse.get_cursor_pos()
                    self._test_log_signal.emit(
                        f"  [{i+1}/{len(points)}] {label}: HID パーク後 "
                        f"({px},{py})  目標パーク ({park_x},{park_y})"
                    )
                except Exception as e:
                    self._test_log_signal.emit(f"    ⚠ パーク例外: {e}")
                    continue
                # 2) SetCursorPos でジャンプ
                try:
                    self._mouse.move_cursor(x, y)
                    time.sleep(0.03)
                    cx, cy = self._mouse.get_cursor_pos()
                    miss = abs(cx - x) > 3 or abs(cy - y) > 3
                    self._test_log_signal.emit(
                        f"    SetCursorPos → 実({cx},{cy})  目標({x},{y})  "
                        f"誤差({cx-x:+d},{cy-y:+d})"
                        + ("  ⚠ ブロックされた可能性" if miss else "")
                    )
                except Exception as e:
                    self._test_log_signal.emit(f"    ⚠ ジャンプ例外: {e}")
                    continue
                # 3) CLICK 送信
                try:
                    resp = self._mouse._cmd("CLICK L 50")
                    self._test_log_signal.emit(f"    CLICK resp={resp}")
                except Exception as e:
                    self._test_log_signal.emit(f"    ⚠ CLICK 例外: {e}")
            self._test_log_signal.emit("--- [B] 完了 ---")

        threading.Thread(target=_worker, daemon=True).start()

    # ---------------------------------------------------------------- 定期更新
    def _refresh_status(self) -> None:
        title = self._settings.get("window_title", "")
        self._update_win_label(title)
        # 本日フロー表の赤線を 1 秒ごとに動かす（実行タブが見えていなくても安価）
        if hasattr(self, "_sched_table"):
            self._sched_table.viewport().update()
        # 日付跨ぎ検出: 表示中の曜日が "今日" のまま固定だった場合に追従させる
        new_today_wd = datetime.now().weekday()
        if getattr(self, "_last_today_wd", None) != new_today_wd:
            prev = getattr(self, "_last_today_wd", None)
            self._last_today_wd = new_today_wd
            # 「今日」を表示していたなら新しい曜日に移動、それ以外はラベルだけ更新
            if prev is not None and self._displayed_weekday == prev:
                self._displayed_weekday = new_today_wd
            self._update_day_label()
            self._refresh_day_list()
            # 深夜 0 時を跨いだのでウォッチャー発火カウントもリセット表示に反映
            self._reload_watchers_list()

    # ---------------------------------------------------------------- ログ
    def _append_log(self, msg: str) -> None:
        self._log_box.append(msg)
        sb = self._log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ---------------------------------------------------------------- 終了
    def closeEvent(self, e) -> None:  # noqa: N802
        self._runner.stop()
        if hasattr(self, "_recorder"):
            self._recorder.stop()
        # 経験値計測 + オーバーレイ
        if hasattr(self, "_exp_meter"):
            self._exp_meter.stop()
            self._exp_meter.save()
        if self._exp_overlay is not None:
            p = self._exp_overlay.pos()
            self._settings["overlay_pos"] = [p.x(), p.y()]
            save_settings(self._settings)
            try:
                self._exp_overlay.close()
            except Exception:
                pass
        # 翻訳ループも停止
        if hasattr(self, "_tr_stop"):
            self._tr_stop.set()
        # 開いている編集ウィンドウを全て閉じる。
        # close() が closed シグナル経由でメインの保持リストから自己除去するため、
        # リスト変動を避けて list() でスナップショットしてから回す。
        for attr in ("_scene_editors", "_watcher_editors", "_flow_editors"):
            for win in list(getattr(self, attr, []) or []):
                try:
                    win.close()
                except Exception:
                    pass
        if self._capture_overlay is not None:
            try:
                self._capture_overlay.close()
            except Exception:
                pass
        if self._mouse:
            try:
                self._mouse.close()
            except Exception:
                pass
        super().closeEvent(e)


class _SettingsDialog(QDialog):
    """設定ダイアログ。Google Chat 通知 + 翻訳タブの API キー / ベース言語。"""

    def __init__(self, settings: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.resize(560, 340)
        self._initial = dict(settings)

        lay = QVBoxLayout(self)

        # Google Chat 通知
        grp = QGroupBox("Google Chat 通知")
        form = QFormLayout(grp)
        self._inp_webhook = QLineEdit(settings.get("google_chat_webhook", "") or "")
        self._inp_webhook.setPlaceholderText(
            "https://chat.googleapis.com/v1/spaces/.../messages?key=...&token=..."
        )
        self._inp_webhook.setToolTip(
            "Google Chat スペースの「アプリと連携」→「Webhook を追加」で発行した URL を貼り付け。"
            "空欄なら通知無効。"
        )
        form.addRow("Webhook URL:", self._inp_webhook)

        test_row = QHBoxLayout()
        test_row.addStretch(1)
        self._btn_test = QPushButton("テスト通知を送信")
        self._btn_test.clicked.connect(self._on_test)
        test_row.addWidget(self._btn_test)
        form.addRow("", test_row)

        self._lbl_test = QLabel("")
        self._lbl_test.setWordWrap(True)
        self._lbl_test.setStyleSheet("color:#555; font-size:11px;")
        form.addRow("", self._lbl_test)

        lay.addWidget(grp)

        # 前面化確認 (ウォッチャー/スケジュール発火時)
        fg_grp = QGroupBox("前面化確認ダイアログ")
        fg_form = QFormLayout(fg_grp)
        self._spin_fg_interval = QDoubleSpinBox()
        self._spin_fg_interval.setRange(0.0, 120.0)
        self._spin_fg_interval.setDecimals(1)
        self._spin_fg_interval.setSingleStep(1.0)
        self._spin_fg_interval.setSuffix(" 分")
        self._spin_fg_interval.setValue(
            float(settings.get("foreground_check_interval_min", 5.0))
        )
        self._spin_fg_interval.setToolTip(
            "対象ウィンドウが前面でない時に出す確認ダイアログの再表示間隔。\n"
            "この時間内に出した選択（即時実施 / スキップ）はキャッシュされ、\n"
            "ウォッチャーが秒単位で発火しても毎回ダイアログが出ません。\n"
            "0 = 毎回確認（キャッシュ無効）。"
        )
        fg_form.addRow("再表示間隔:", self._spin_fg_interval)
        fg_hint = QLabel(
            "ウォッチャーが連続発火しても、この間隔内は直近の選択を自動適用します。\n"
            "「待機」は対象外（毎回 3 分後に再評価）。"
        )
        fg_hint.setStyleSheet("color:#666; font-size:11px;")
        fg_hint.setWordWrap(True)
        fg_form.addRow("", fg_hint)
        lay.addWidget(fg_grp)

        # 実行中オーバーレイ
        ov_grp = QGroupBox("フロー実行中オーバーレイ")
        ov_form = QFormLayout(ov_grp)
        self._chk_overlay = QCheckBox("フロー開始時に半透明オーバーレイを自動表示")
        self._chk_overlay.setChecked(
            bool(settings.get("flow_overlay_enabled", True))
        )
        self._chk_overlay.setToolTip(
            "実行中シーン名 / ステップ進捗 / 次回スケジュール / 直近の発火 を\n"
            "ゲームウィンドウの上に常時前面で重ねて表示します。\n"
            "オーバーレイは左ドラッグで移動可、位置は自動保存されます。"
        )
        ov_form.addRow("", self._chk_overlay)
        lay.addWidget(ov_grp)

        # 翻訳タブ
        from .translation import LANG_CODES, LANG_LABELS_JA
        tr_grp = QGroupBox("翻訳タブ (Claude API)")
        tr_form = QFormLayout(tr_grp)
        self._inp_tr_key = QLineEdit(settings.get("translation_api_key", "") or "")
        self._inp_tr_key.setEchoMode(QLineEdit.Password)
        self._inp_tr_key.setPlaceholderText("sk-ant-api03-...")
        self._inp_tr_key.setToolTip(
            "Anthropic Console (console.anthropic.com) で発行した API キーを貼り付け。\n"
            "空欄なら翻訳タブは無効。"
        )
        tr_form.addRow("Claude API キー:", self._inp_tr_key)

        self._cmb_tr_base = QComboBox()
        base_cur = (settings.get("translation_base_lang") or "ja").lower()
        for code in LANG_CODES:
            self._cmb_tr_base.addItem(f"{LANG_LABELS_JA[code]} ({code})", code)
            if code == base_cur:
                self._cmb_tr_base.setCurrentIndex(self._cmb_tr_base.count() - 1)
        self._cmb_tr_base.setToolTip(
            "チャット領域のメッセージはこのベース言語に統一して翻訳されます。\n"
            "ベース言語と同じメッセージは原文のまま表示。"
        )
        tr_form.addRow("ベース言語:", self._cmb_tr_base)

        hint_tr = QLabel(
            "領域・間隔・ユーザー入力の対象言語は翻訳タブ内で個別に変更します。"
        )
        hint_tr.setStyleSheet("color:#666; font-size:11px;")
        hint_tr.setWordWrap(True)
        tr_form.addRow("", hint_tr)
        lay.addWidget(tr_grp)

        # OK / Cancel
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _on_test(self) -> None:
        url = self._inp_webhook.text().strip()
        if not url:
            self._lbl_test.setStyleSheet("color:#c62828; font-size:11px;")
            self._lbl_test.setText("⚠ Webhook URL を入力してください")
            return
        self._lbl_test.setStyleSheet("color:#555; font-size:11px;")
        self._lbl_test.setText("送信中…")
        QApplication.processEvents()
        ok, msg = send_google_chat(
            url,
            "テスト通知",
            "Nightcrows 自動化ツールからのテスト送信です。\n"
            "この通知が見えていれば、Webhook 設定は正しく動いています。",
        )
        if ok:
            self._lbl_test.setStyleSheet("color:#2e7d32; font-size:11px;")
            self._lbl_test.setText(f"✓ 送信成功 ({msg})")
        else:
            self._lbl_test.setStyleSheet("color:#c62828; font-size:11px;")
            self._lbl_test.setText(f"✗ 送信失敗: {msg}")

    def result_settings(self) -> dict:
        """OK 時に呼ばれて、変更されたキーを含む dict を返す。"""
        return {
            "google_chat_webhook": self._inp_webhook.text().strip(),
            "foreground_check_interval_min": float(self._spin_fg_interval.value()),
            "flow_overlay_enabled": bool(self._chk_overlay.isChecked()),
            "translation_api_key": self._inp_tr_key.text().strip(),
            "translation_base_lang": self._cmb_tr_base.currentData() or "ja",
        }


class _RunDayTable(QTableWidget):
    """実行タブ用: 24時間 × 1列の本日フロー表。現在時刻に赤線を引く。

    pc_flow_editor の _ScheduleTable と同じ要領の赤線描画を、1時間刻みに合わせて行う。
    """

    HOURS = 24
    ROW_H = 26

    def __init__(self) -> None:
        super().__init__(self.HOURS, 1)
        # 縦ヘッダー: HH:00、横ヘッダー: 「本日のフロー」
        self.setVerticalHeaderLabels([f"{h:02d}:00" for h in range(self.HOURS)])
        self.setHorizontalHeaderLabels(["本日のフロー"])
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionMode(QTableWidget.NoSelection)
        self.setShowGrid(True)
        hdr = self.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Stretch)
        for r in range(self.HOURS):
            self.setRowHeight(r, self.ROW_H)
        self.setStyleSheet("font-size:12px;")

    def paintEvent(self, e) -> None:  # noqa: N802
        super().paintEvent(e)
        # 1 行 = 1 時間。現在時刻 → 行内の比率（分・秒で滑らかに）
        now = datetime.now()
        row_f = now.hour + now.minute / 60.0 + now.second / 3600.0
        if row_f < 0 or row_f >= self.rowCount():
            return
        row = int(row_f)
        frac = row_f - row
        y_top = self.rowViewportPosition(row)
        if y_top < 0:
            return
        y = y_top + int(frac * self.rowHeight(row))
        x_start = self.columnViewportPosition(0)
        last = self.columnCount() - 1
        x_end = self.columnViewportPosition(last) + self.columnWidth(last)
        p = QPainter(self.viewport())
        pen = QPen(QColor("#e53935"))
        pen.setWidth(2)
        p.setPen(pen)
        p.drawLine(x_start, y, x_end, y)


class _ClickCaptureOverlay(QWidget):
    """全画面の透明オーバーレイ。左クリックで座標を連続取得する。

    - 左クリック: クリック位置を `clicked` シグナルで通知（オーバーレイは閉じない）
    - 右クリック / ESC: `finished` シグナル → 閉じる
    """

    clicked  = Signal(int, int)   # クリック位置 (x, y)
    finished = Signal()           # 取得モード終了

    def __init__(self) -> None:
        super().__init__(None)
        # Qt.Tool を外し標準的なフローティングウィンドウとして扱う。
        # 一部の Windows 環境では Tool フラグ付きフレームレス透明ウィンドウが
        # 表示されない（タスクバーにも出ず最前面化されない）事があるため。
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)

        # 仮想スクリーン全体を覆う
        screen = QApplication.primaryScreen()
        if screen is not None:
            vg = screen.virtualGeometry()
            self.setGeometry(vg)
        else:
            # フォールバック: プライマリ画面が取れないケース
            self.setGeometry(0, 0, 1920, 1080)

        self._count = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.addStretch(1)
        self._lbl = QLabel()
        self._lbl.setAlignment(Qt.AlignCenter)
        self._lbl.setStyleSheet(
            "background:rgba(0,0,0,200); color:white; padding:12px 24px;"
            "font-size:14px; font-weight:bold; border-radius:8px;"
            "border: 2px solid #ffeb3b;"
        )
        self._update_label()
        lay.addWidget(self._lbl, 0, Qt.AlignHCenter)
        lay.addStretch(1)

    def _update_label(self) -> None:
        self._lbl.setText(
            f"🎯 左クリック=取得 ({self._count}件)  /  右クリック・ESC=終了"
        )

    def paintEvent(self, e) -> None:  # noqa: N802
        # 薄く塗ってオーバーレイの存在を分かりやすく（以前より濃いめ）
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 80))

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
        self.raise_()
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
