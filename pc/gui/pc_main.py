"""PC フロー制御 メインウィンドウ。"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .exp_meter import DEFAULT_LOG_RETAIN_DAYS, ExpMeter
from .pc_flow import (
    DAY_NAMES,
    FLOWS_DIR,
    PcFlowRunner,
    entry_scenes,
    load_pc_flow,
)
from .settings import load_settings, save_settings
from .window_picker import WindowPickerDialog, find_hwnd_by_title

try:
    from pico_mouse import PicoMouse, find_pico_port
except ImportError:
    PicoMouse = None        # type: ignore[assignment,misc]
    find_pico_port = None   # type: ignore[assignment]


class PcFlowWindow(QWidget):
    """PC フロー制御メインウィンドウ。"""

    def __init__(self, settings: dict) -> None:
        super().__init__()
        self._settings = settings
        self._mouse: "PicoMouse | None" = None
        self._runner = PcFlowRunner()
        self._meter = ExpMeter()

        self.setWindowTitle("PC フロー制御")
        self.setMinimumWidth(560)

        self._build_ui()
        self._connect_signals()
        self._setup_meter()
        self._load_flows_list()
        self._restore_settings()

        QTimer.singleShot(400, self._auto_connect_pico)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_status)
        self._refresh_timer.start(1000)

    # ---------------------------------------------------------------- UI 構築
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        # ─── ゲームウィンドウ ────────────────────────────
        grp_win = QGroupBox("ゲームウィンドウ")
        win_lay = QHBoxLayout(grp_win)
        self._lbl_win = QLabel("未設定")
        self._btn_win = QPushButton("選択…")
        self._btn_win.setFixedWidth(72)
        self._btn_win.clicked.connect(self._pick_window)
        win_lay.addWidget(self._lbl_win, 1)
        win_lay.addWidget(self._btn_win)
        outer.addWidget(grp_win)

        # ─── Pico マウス ─────────────────────────────────
        grp_pico = QGroupBox("Pico マウス")
        pico_lay = QHBoxLayout(grp_pico)
        self._lbl_pico = QLabel("未接続")
        self._btn_pico = QPushButton("接続")
        self._btn_pico.setFixedWidth(72)
        self._btn_pico.clicked.connect(self._connect_pico)
        pico_lay.addWidget(self._lbl_pico, 1)
        pico_lay.addWidget(self._btn_pico)
        outer.addWidget(grp_pico)

        # ─── フロー制御 ───────────────────────────────────
        grp_flow = QGroupBox("フロー")
        flow_lay = QVBoxLayout(grp_flow)

        flow_row = QHBoxLayout()
        self._combo_flow = QComboBox()
        self._combo_flow.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._combo_flow.currentIndexChanged.connect(self._on_flow_selected)
        self._btn_flow = QPushButton("開始")
        self._btn_flow.setFixedWidth(64)
        self._btn_flow.clicked.connect(self._toggle_flow)
        flow_row.addWidget(QLabel("フロー:"))
        flow_row.addWidget(self._combo_flow, 1)
        flow_row.addWidget(self._btn_flow)
        flow_lay.addLayout(flow_row)

        self._lbl_run_status = QLabel("待機中")
        self._lbl_run_status.setStyleSheet("color:#555; font-size:11px;")
        flow_lay.addWidget(self._lbl_run_status)

        flow_lay.addWidget(QLabel("スケジュール:"))
        self._list_sched = QListWidget()
        self._list_sched.setMaximumHeight(110)
        self._list_sched.setStyleSheet("font-size:11px;")
        flow_lay.addWidget(self._list_sched)

        self._lbl_next_sched = QLabel("")
        self._lbl_next_sched.setStyleSheet("color:#1565c0; font-size:11px;")
        flow_lay.addWidget(self._lbl_next_sched)

        outer.addWidget(grp_flow)

        # ─── 経験値メーター ───────────────────────────────
        grp_exp = QGroupBox("経験値メーター")
        exp_lay = QVBoxLayout(grp_exp)
        self._lbl_exp_cur  = QLabel("現在値:  —")
        self._lbl_exp_spd  = QLabel("速度:  —")
        self._lbl_exp_acc  = QLabel("累計:  —")
        for lbl in (self._lbl_exp_cur, self._lbl_exp_spd, self._lbl_exp_acc):
            exp_lay.addWidget(lbl)
        exp_btn_row = QHBoxLayout()
        self._btn_exp = QPushButton("計測開始")
        self._btn_exp.clicked.connect(self._toggle_exp)
        self._btn_exp_reset = QPushButton("リセット")
        self._btn_exp_reset.clicked.connect(self._reset_exp)
        exp_btn_row.addWidget(self._btn_exp)
        exp_btn_row.addWidget(self._btn_exp_reset)
        exp_btn_row.addStretch()
        exp_lay.addLayout(exp_btn_row)
        outer.addWidget(grp_exp)

        # ─── ログ ─────────────────────────────────────────
        grp_log = QGroupBox("ログ")
        log_lay = QVBoxLayout(grp_log)
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(150)
        self._log_box.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        log_lay.addWidget(self._log_box)
        outer.addWidget(grp_log)

    def _connect_signals(self) -> None:
        self._runner.log_message.connect(self._append_log)
        self._runner.state_changed.connect(self._on_state_changed)
        self._runner.next_schedule_changed.connect(self._lbl_next_sched.setText)
        self._runner.scene_started.connect(self._on_scene_started)
        self._runner.step_updated.connect(self._on_step_updated)
        self._meter.updated.connect(self._refresh_exp)

    # ---------------------------------------------------------------- 設定復元
    def _setup_meter(self) -> None:
        retain = int(self._settings.get("log_retain_days", DEFAULT_LOG_RETAIN_DAYS))
        ExpMeter.purge_old_logs(retain)

    def _restore_settings(self) -> None:
        title = self._settings.get("window_title", "")
        if title:
            self._runner.set_window_title(title)
            self._update_win_label(title)
        self._meter.window_title = title
        region = self._settings.get("region_rel")
        if region:
            self._meter.region_rel = region
        self._meter.digit_hint = int(self._settings.get("digit_hint", 1))

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
            self._meter.window_title = title
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
            return

        if self._mouse:
            try:
                self._mouse.close()
            except Exception:
                pass
            self._mouse = None

        try:
            self._mouse = PicoMouse()
            self._runner.set_mouse(self._mouse)
            self._lbl_pico.setText(f"✓ 接続済 {self._mouse.port}")
            self._lbl_pico.setStyleSheet("color:#2e7d32;")
            self._btn_pico.setText("再接続")
            self._append_log(f"Pico 接続: {self._mouse.port}")
        except Exception as e:
            self._lbl_pico.setText(f"✗ 接続失敗: {e}")
            self._lbl_pico.setStyleSheet("color:#c62828;")
            self._append_log(f"Pico 接続エラー: {e}")

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

    # ---------------------------------------------------------------- 経験値メーター
    def _toggle_exp(self) -> None:
        if self._meter.running:
            self._meter.stop()
            self._btn_exp.setText("計測開始")
            self._btn_exp.setStyleSheet("")
        else:
            if self._meter.start():
                self._btn_exp.setText("計測停止")
                self._btn_exp.setStyleSheet(
                    "QPushButton{background:#c62828;color:white;}"
                )

    def _reset_exp(self) -> None:
        self._meter.reset()

    def _refresh_exp(self) -> None:
        m = self._meter
        self._lbl_exp_cur.setText(
            f"現在値:  {m.prev_raw:.4f}%" if m.prev_raw is not None else "現在値:  —"
        )
        cur = m.current_speed()
        self._lbl_exp_spd.setText(
            f"速度:  {cur:+.2f} %/h" if cur is not None else "速度:  —"
        )
        if m.samples and len(m.samples) >= 2:
            acc = m.samples[-1][1] - m.samples[0][1]
            self._lbl_exp_acc.setText(f"累計:  {acc:+.4f}%")
        else:
            self._lbl_exp_acc.setText("累計:  —")

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
        self._meter.stop()
        if self._mouse:
            try:
                self._mouse.close()
            except Exception:
                pass
        super().closeEvent(e)


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    settings = load_settings()
    win = PcFlowWindow(settings)
    win.show()
    sys.exit(app.exec())
