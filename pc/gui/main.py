"""PC 経験値メーターのエントリーポイント。

- 起動時: 設定ウィンドウ表示
- 計測開始: 設定ウィンドウを閉じ、コンパクトオーバーレイ表示
- オーバーレイ ダブルクリック / 右クリックメニュー: 設定ウィンドウ復帰
"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .exp_meter import CURRENT_WINDOW, DEFAULT_LOG_RETAIN_DAYS, ExpMeter
from .overlay import OverlayWindow
from .region_picker import RegionPickerDialog
from .settings import EXP_METER_PATH, load_settings, save_settings
from .tesseract import (
    INSTALLER_URL,
    WINGET_CMD,
    apply_path,
    detect_tesseract,
    get_version,
)
from .window_picker import WindowPickerDialog, find_hwnd_by_title


class SetupWindow(QWidget):
    """設定ウィンドウ。計測状況の詳細もここで確認できる。"""

    def __init__(self, meter: ExpMeter, settings: dict) -> None:
        super().__init__()
        self._meter    = meter
        self._settings = settings
        self.setWindowTitle("経験値メーター — 設定")
        self.setFixedSize(600, 460)

        self._overlay: OverlayWindow | None = None

        self._build_ui()
        self._refresh()
        meter.updated.connect(self._refresh)
        meter.status_changed.connect(self._on_status)
        meter.error.connect(self._on_error)

        # Tesseract: 設定にあればそれを優先、なければ自動検出
        self._setup_tesseract()

        # 古いログを起動時に1回掃除
        retain = int(self._settings.get("log_retain_days", DEFAULT_LOG_RETAIN_DAYS))
        ExpMeter.purge_old_logs(retain)

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        # --- 設定（一度設定すれば次回から不要）
        grp_cfg = QGroupBox("⚙ 設定（一度行えば次回以降は不要）")
        cfg_lay = QFormLayout(grp_cfg)

        # 対象ウィンドウ: 状態表示 + 変更ボタン
        win_row = QHBoxLayout()
        self._lbl_win = QLabel("")
        self._btn_win = QPushButton("変更…")
        self._btn_win.clicked.connect(self._pick_window)
        win_row.addWidget(self._lbl_win, 1)
        win_row.addWidget(self._btn_win)
        cfg_lay.addRow("🪟 対象ウィンドウ:", win_row)

        # 計測領域: 状態表示 + 変更ボタン
        reg_row = QHBoxLayout()
        self._lbl_region = QLabel("")
        self._btn_region = QPushButton("変更…")
        self._btn_region.clicked.connect(self._pick_region)
        reg_row.addWidget(self._lbl_region, 1)
        reg_row.addWidget(self._btn_region)
        cfg_lay.addRow("📍 計測領域:", reg_row)

        # 桁数ヒント
        hint_row = QHBoxLayout()
        self._rb1 = QRadioButton("1桁 (0〜9%台)")
        self._rb2 = QRadioButton("2桁 (10〜99%台)")
        (self._rb2 if self._meter.digit_hint == 2 else self._rb1).setChecked(True)
        self._rb1.toggled.connect(self._on_hint_changed)
        hint_row.addWidget(self._rb1)
        hint_row.addWidget(self._rb2)
        hint_row.addStretch()
        cfg_lay.addRow("桁数ヒント:", hint_row)

        # 計測間隔（OCRは1サンプル ~5秒かかるので最小10秒）
        interval_row = QHBoxLayout()
        self._spin_interval = QSpinBox()
        self._spin_interval.setRange(10, 600)
        self._spin_interval.setSingleStep(5)
        self._spin_interval.setSuffix(" 秒")
        self._spin_interval.setValue(self._meter.interval_sec)
        self._spin_interval.valueChanged.connect(self._on_interval_changed)
        interval_row.addWidget(self._spin_interval)
        interval_row.addStretch()
        cfg_lay.addRow("⏱ 計測間隔:", interval_row)

        # ログ保持日数
        log_row = QHBoxLayout()
        self._spin_log_days = QSpinBox()
        self._spin_log_days.setRange(1, 365)
        self._spin_log_days.setSuffix(" 日")
        self._spin_log_days.setValue(
            int(self._settings.get("log_retain_days", DEFAULT_LOG_RETAIN_DAYS))
        )
        self._spin_log_days.valueChanged.connect(self._on_log_days_changed)
        log_row.addWidget(self._spin_log_days)
        log_row.addStretch()
        cfg_lay.addRow("🗒 ログ保持:", log_row)

        outer.addWidget(grp_cfg)

        # --- 計測状況
        grp_stat = QGroupBox("📊 計測状況")
        stat_lay = QVBoxLayout(grp_stat)
        self._lbl_current = QLabel("現在値:  —")
        self._lbl_cspd    = QLabel("現在速度:  —")
        self._lbl_aspd    = QLabel("平均速度:  —")
        self._lbl_eta     = QLabel("LvUP予測:  —")
        self._lbl_meta    = QLabel("")
        self._lbl_meta.setStyleSheet("color:#777; font-size:10px;")
        for w in (self._lbl_current, self._lbl_cspd, self._lbl_aspd, self._lbl_eta, self._lbl_meta):
            stat_lay.addWidget(w)
        outer.addWidget(grp_stat)

        # --- コントロール
        ctrl = QHBoxLayout()
        self._btn_start = QPushButton("▶ 計測開始")
        self._btn_start.clicked.connect(self._toggle_running)
        self._btn_reset = QPushButton("🔄 リセット")
        self._btn_reset.clicked.connect(self._reset)
        self._btn_overlay = QPushButton("🗕 ゲーム画面に重ねる")
        self._btn_overlay.clicked.connect(self._switch_to_overlay)
        self._btn_quit = QPushButton("✕ 終了")
        self._btn_quit.setStyleSheet("QPushButton{background:#555;color:white;}")
        self._btn_quit.clicked.connect(self._quit_app)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color:#666; font-size:11px;")
        ctrl.addWidget(self._btn_start)
        ctrl.addWidget(self._btn_reset)
        ctrl.addWidget(self._btn_overlay)
        ctrl.addWidget(self._status_lbl, 1)
        ctrl.addWidget(self._btn_quit)
        outer.addLayout(ctrl)

        # --- OCR ステータス（最下段、最小限の表示）
        ocr_row = QHBoxLayout()
        self._lbl_ocr = QLabel("OCR: 確認中…")
        self._lbl_ocr.setStyleSheet("color:#666; font-size:11px;")
        self._btn_ocr_setup = QPushButton("インストール方法…")
        self._btn_ocr_setup.clicked.connect(self._show_tess_install)
        self._btn_ocr_change = QPushButton("変更…")
        self._btn_ocr_change.clicked.connect(self._change_tess_path)
        ocr_row.addWidget(self._lbl_ocr, 1)
        ocr_row.addWidget(self._btn_ocr_change)
        ocr_row.addWidget(self._btn_ocr_setup)
        outer.addLayout(ocr_row)

    # ---------------------------------------------------------------- ハンドラ
    def _on_hint_changed(self, *_) -> None:
        self._meter.digit_hint = 2 if self._rb2.isChecked() else 1
        self._meter.save()

    def _on_interval_changed(self, v: int) -> None:
        self._meter.set_interval(v)

    def _on_log_days_changed(self, v: int) -> None:
        self._settings["log_retain_days"] = int(v)
        save_settings(self._settings)

    def _pick_window(self) -> None:
        dlg = WindowPickerDialog(self._meter.window_title, self)
        if dlg.exec():
            t = dlg.selected_title()
            self._meter.window_title = t
            self._settings["window_title"] = t
            save_settings(self._settings)
            self._meter.save()
            self._refresh()

    def _pick_region(self) -> None:
        title = self._meter.window_title
        if not title:
            QMessageBox.information(self, "情報", "先に対象ウィンドウを指定してください")
            return
        dlg = RegionPickerDialog(title, self._meter.region_rel, self)
        if dlg.exec():
            r = dlg.get_rel()
            if r:
                self._meter.region_rel = r
                self._meter.save()
                self._refresh()

    def _toggle_running(self) -> None:
        if self._meter.running:
            self._meter.stop()
            self._refresh()
        else:
            if self._meter.start():
                self._show_overlay()
                self.hide()

    def _switch_to_overlay(self) -> None:
        """計測中なら設定を閉じてオーバーレイへ。停止中なら何もしない（ボタン無効化済み）。"""
        if not self._meter.running:
            return
        self._show_overlay()
        self.hide()

    def closeEvent(self, e) -> None:  # noqa: N802
        """X ボタンも明示的な終了として扱う。計測中なら確認。"""
        if getattr(self, "_quitting", False):
            super().closeEvent(e)
            return
        if not self._confirm_quit():
            e.ignore()
            return
        self._do_quit()
        super().closeEvent(e)

    def _reset(self) -> None:
        if QMessageBox.question(self, "確認", "サンプルと累積値をリセットしますか？") == QMessageBox.Yes:
            self._meter.reset()

    # ---------------------------------------------------------------- オーバーレイ連携
    def _show_overlay(self) -> None:
        if self._overlay is None:
            self._overlay = OverlayWindow(self._meter)
            self._overlay.request_setup.connect(self._return_to_setup)
            self._overlay.request_toggle.connect(self._toggle_running)
            self._overlay.request_reset.connect(self._reset)
            self._overlay.request_quit.connect(self._quit_app)
        # 位置復元 or 右上にデフォルト配置
        pos = self._settings.get("overlay_pos")
        if pos and isinstance(pos, list) and len(pos) == 2:
            self._overlay.move(int(pos[0]), int(pos[1]))
        else:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            self._overlay.adjustSize()
            self._overlay.move(screen.right() - self._overlay.width() - 16, screen.top() + 16)
        self._overlay.show()

    def _return_to_setup(self) -> None:
        if self._overlay:
            # 位置を保存
            p = self._overlay.pos()
            self._settings["overlay_pos"] = [p.x(), p.y()]
            save_settings(self._settings)
            self._overlay.hide()
        self.show()
        self.raise_()
        self.activateWindow()

    def _confirm_quit(self) -> bool:
        """計測中のみ確認ダイアログ。停止中はそのまま True。"""
        if not self._meter.running:
            return True
        ret = QMessageBox.question(
            self, "終了確認",
            "計測中です。アプリを終了しますか？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return ret == QMessageBox.Yes

    def _do_quit(self) -> None:
        """終了処理本体。状態保存→stop→ウィンドウ閉→quit。"""
        self._quitting = True
        if self._overlay is not None:
            p = self._overlay.pos()
            self._settings["overlay_pos"] = [p.x(), p.y()]
            save_settings(self._settings)
            self._overlay.close()
            self._overlay = None
        self._meter.stop()
        app = QApplication.instance()
        app.closeAllWindows()
        app.quit()

    def _quit_app(self) -> None:
        if not self._confirm_quit():
            return
        self._do_quit()

    # ---------------------------------------------------------------- 表示更新
    def _refresh(self) -> None:
        m = self._meter
        self._btn_start.setText("■ 停止" if m.running else "▶ 計測開始")
        self._btn_start.setStyleSheet(
            "QPushButton{background:#c62828;color:white;font-weight:bold;}"
            if m.running else ""
        )
        self._btn_overlay.setEnabled(m.running)

        # 対象ウィンドウ ステータス
        if not m.window_title:
            self._lbl_win.setText("⚠ 未設定")
            self._lbl_win.setStyleSheet("color:#c62828;")
        elif find_hwnd_by_title(m.window_title):
            self._lbl_win.setText(f"✓ {m.window_title}")
            self._lbl_win.setStyleSheet("color:#2e7d32;")
        else:
            self._lbl_win.setText(f"⚠ {m.window_title}（見つかりません）")
            self._lbl_win.setStyleSheet("color:#ef6c00;")

        # 計測領域 ステータス
        if m.region_rel:
            r = m.region_rel
            self._lbl_region.setText(
                f"✓ 設定済み  (x={r[0]:.3f} y={r[1]:.3f} w={r[2]:.3f} h={r[3]:.3f})"
            )
            self._lbl_region.setStyleSheet("color:#2e7d32;")
        else:
            self._lbl_region.setText("⚠ 未設定")
            self._lbl_region.setStyleSheet("color:#c62828;")

        self._lbl_current.setText(
            f"現在値:  {m.prev_raw:.4f}%" if m.prev_raw is not None else "現在値:  —"
        )

        cur = m.current_speed()
        avg = m.avg_speed()
        n = len(m.samples)
        if cur is not None:
            self._lbl_cspd.setText(f"現在速度:  {cur:.1f} %/h")
        elif n > 0:
            self._lbl_cspd.setText(f"現在速度:  — (あと{CURRENT_WINDOW - n}サンプル)")
        else:
            self._lbl_cspd.setText("現在速度:  —")
        self._lbl_aspd.setText(f"平均速度:  {avg:.1f} %/h" if avg is not None else "平均速度:  —")

        eta_cur, eta_avg = m.eta_to_levelup()
        parts: list[str] = []
        for e, lab in ((eta_cur, "現在"), (eta_avg, "平均")):
            if e is None:
                continue
            if e >= 60:
                h, mn = divmod(int(e), 60)
                parts.append(f"{h}h{mn:02d}m（{lab}）")
            else:
                parts.append(f"{int(e)}分（{lab}）")
        self._lbl_eta.setText(
            "LvUP予測:  " + "  /  ".join(parts) if parts else "LvUP予測:  —"
        )

        last = m.samples[-1][0].strftime("%H:%M") if m.samples else "—"
        self._lbl_meta.setText(f"計測: {m.elapsed_str()}  {n}サンプル  最終: {last}")

    def _on_status(self, s: str) -> None:
        self._status_lbl.setText(s)

    def _on_error(self, s: str) -> None:
        QMessageBox.warning(self, "エラー", s)

    # ---------------------------------------------------------------- Tesseract
    def _setup_tesseract(self) -> None:
        """設定優先 → 自動検出。見つからなければ初回案内ダイアログ。"""
        cmd = (self._settings.get("tesseract_cmd") or "").strip()
        if cmd and os.path.isfile(cmd):
            apply_path(cmd)
        else:
            found = detect_tesseract()
            if found:
                apply_path(found)
                # 自動検出結果を保存しておく
                self._settings["tesseract_cmd"] = found
                save_settings(self._settings)
            else:
                # 起動直後の案内
                QTimer.singleShot(200, self._show_tess_install)
        self._update_ocr_status()

    def _update_ocr_status(self) -> None:
        cmd = (self._settings.get("tesseract_cmd") or "").strip()
        if cmd and os.path.isfile(cmd):
            ver = get_version(cmd) or "?"
            self._lbl_ocr.setText(f"OCR: ✓ {ver}")
            self._lbl_ocr.setStyleSheet("color:#2e7d32; font-size:11px;")
            self._btn_ocr_setup.setVisible(False)
            self._btn_ocr_change.setVisible(True)
        else:
            self._lbl_ocr.setText("OCR: ✗ Tesseract が見つかりません（計測には必須）")
            self._lbl_ocr.setStyleSheet("color:#c62828; font-size:11px; font-weight:bold;")
            self._btn_ocr_setup.setVisible(True)
            self._btn_ocr_change.setVisible(True)

    def _show_tess_install(self) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Tesseract OCR をインストールしてください")
        box.setTextFormat(Qt.RichText)
        box.setText(
            "<b>経験値の読み取りには Tesseract OCR が必要です。</b><br><br>"
            "<b>方法1: インストーラー（推奨）</b><br>"
            f'<a href="{INSTALLER_URL}">{INSTALLER_URL}</a><br>'
            "→ ページ内の最新インストーラ（tesseract-ocr-w64-setup-*.exe）をDLして実行<br>"
            "→ 日本語が必要なら言語パックで「Japanese」にチェック<br><br>"
            "<b>方法2: winget</b><br>"
            f"<code>{WINGET_CMD}</code><br><br>"
            "インストール後、このダイアログを閉じて「変更…」ボタンから "
            "tesseract.exe を選択するか、再起動すると自動検出されます。"
        )
        box.setStandardButtons(QMessageBox.Ok)
        box.exec()

    def _change_tess_path(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "tesseract.exe を選択",
            r"C:\Program Files\Tesseract-OCR",
            "実行ファイル (*.exe)"
        )
        if not path:
            return
        if not os.path.isfile(path):
            QMessageBox.warning(self, "エラー", "ファイルが見つかりません")
            return
        self._settings["tesseract_cmd"] = path
        save_settings(self._settings)
        apply_path(path)
        self._update_ocr_status()


def main() -> None:
    app = QApplication(sys.argv)
    settings = load_settings()
    meter = ExpMeter()
    win = SetupWindow(meter, settings)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
