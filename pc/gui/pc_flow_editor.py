"""PC フロー編集ウィンドウ（独立、約 1100x800）。

週間スケジュールを 7 曜日 × 48 スロット (30 分刻み) の QTableWidget で
俯瞰し、セルダブルクリックで新規/編集ダイアログを開いて編集する。

データモデルは pc_flow.PcFlow / ScheduleEntry をそのまま使う。
"""
from __future__ import annotations

import os
from datetime import datetime

from PySide6.QtCore import QDate, Qt, QTime, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from .pc_flow import (
    DAY_NAMES,
    FLOWS_DIR,
    PcFlow,
    ScheduleEntry,
    entry_scenes,
    load_pc_flow,
    save_pc_flow,
)
from .pc_scene import SCENES_DIR, load_pc_scene


# ============================================================ 週間スケジュールテーブル
class _ScheduleTable(QTableWidget):
    """現在時刻に赤線を引く QTableWidget サブクラス。"""

    def paintEvent(self, e) -> None:  # noqa: N802
        super().paintEvent(e)
        # 現在時刻の Y 位置を 30 分スロット単位の浮動小数で計算
        now = datetime.now()
        row_f = now.hour * 2 + now.minute / 30.0 + now.second / 1800.0
        if row_f < 0 or row_f >= self.rowCount():
            return
        row = int(row_f)
        frac = row_f - row
        y_top = self.rowViewportPosition(row)
        if y_top < 0:
            return
        y = y_top + int(frac * self.rowHeight(row))
        # 行全幅で水平赤線（最初の列の左端から最後の列の右端まで）
        if self.columnCount() == 0:
            return
        x_start = self.columnViewportPosition(0)
        last = self.columnCount() - 1
        x_end = self.columnViewportPosition(last) + self.columnWidth(last)
        p = QPainter(self.viewport())
        pen = QPen(QColor("#e53935"))
        pen.setWidth(2)
        p.setPen(pen)
        p.drawLine(x_start, y, x_end, y)


# ============================================================ エントリ編集ダイアログ
class _EntryDialog(QDialog):
    """ScheduleEntry の追加 / 編集ダイアログ。"""

    def __init__(
        self,
        entry: ScheduleEntry,
        scenes_list: list[str],
        parent: QWidget | None = None,
        mode: str = "normal",   # "normal" | "seq"（時刻指定 / 続けて実行）
    ) -> None:
        super().__init__(parent)
        self._mode = mode
        if mode == "seq":
            self.setWindowTitle("続けて実行エントリ")
            self.resize(420, 200)
        else:
            self.setWindowTitle("スケジュールエントリ")
            self.resize(420, 320)
        self.entry = entry   # 編集中のインスタンス（OK 時に書き戻し）

        lay = QVBoxLayout(self)
        form = QFormLayout()

        # 時刻 (normal のみ表示)
        self._te_time = QTimeEdit()
        self._te_time.setDisplayFormat("HH:mm")
        try:
            h, m = map(int, entry.time.split(":"))
            self._te_time.setTime(QTime(h, m))
        except (ValueError, AttributeError):
            self._te_time.setTime(QTime(12, 0))
        if mode == "normal":
            form.addRow("時刻:", self._te_time)
        else:
            # seq モードでは説明だけ示し、time は元値を維持
            note = QLabel("直前の時刻エントリ終了後にすぐ実行されます")
            note.setStyleSheet("color:#1565c0; font-size:11px;")
            note.setWordWrap(True)
            form.addRow("実行タイミング:", note)

        # シーン
        self._cmb_scene = QComboBox()
        self._cmb_scene.addItem("(未選択)", "")
        for s in scenes_list:
            self._cmb_scene.addItem(s, s)
        idx = self._cmb_scene.findData(entry.target)
        if idx >= 0:
            self._cmb_scene.setCurrentIndex(idx)
        form.addRow("シーン:", self._cmb_scene)

        # 繰り返し / 曜日 / 日付 は normal モードのみ
        self._rb_daily  = QRadioButton("毎日")
        self._rb_weekly = QRadioButton("週次")
        self._rb_once   = QRadioButton("1回限り")
        self._rep_group = QButtonGroup(self)
        self._rep_group.addButton(self._rb_daily)
        self._rep_group.addButton(self._rb_weekly)
        self._rep_group.addButton(self._rb_once)
        if entry.repeat == "weekly":
            self._rb_weekly.setChecked(True)
        elif entry.repeat == "once":
            self._rb_once.setChecked(True)
        else:
            self._rb_daily.setChecked(True)

        self._chk_days: list[QCheckBox] = []
        for i, name in enumerate(DAY_NAMES):
            chk = QCheckBox(name)
            chk.setChecked(i in (entry.days or []))
            self._chk_days.append(chk)

        self._de_date = QDateEdit()
        self._de_date.setDisplayFormat("yyyy-MM-dd")
        try:
            d = datetime.strptime(entry.date, "%Y-%m-%d").date()
            self._de_date.setDate(QDate(d.year, d.month, d.day))
        except (ValueError, TypeError, AttributeError):
            self._de_date.setDate(QDate.currentDate())

        if mode == "normal":
            rep_row = QHBoxLayout()
            rep_row.addWidget(self._rb_daily)
            rep_row.addWidget(self._rb_weekly)
            rep_row.addWidget(self._rb_once)
            rep_row.addStretch(1)
            form.addRow("繰り返し:", rep_row)
            day_row = QHBoxLayout()
            for chk in self._chk_days:
                day_row.addWidget(chk)
            form.addRow("曜日:", day_row)
            form.addRow("日付:", self._de_date)

        # 有効
        self._chk_enabled = QCheckBox("有効")
        self._chk_enabled.setChecked(entry.enabled)
        form.addRow("", self._chk_enabled)

        lay.addLayout(form)

        # OK / Cancel
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _on_ok(self) -> None:
        self.entry.target = self._cmb_scene.currentData() or ""
        self.entry.enabled = self._chk_enabled.isChecked()
        if self._mode == "seq":
            # seq では time/repeat/days/date は使われないが、念のため明示
            self.entry.seq = True
        else:
            self.entry.seq = False
            self.entry.time = self._te_time.time().toString("HH:mm")
            if self._rb_weekly.isChecked():
                self.entry.repeat = "weekly"
                self.entry.days = [i for i, c in enumerate(self._chk_days) if c.isChecked()]
            elif self._rb_once.isChecked():
                self.entry.repeat = "once"
                self.entry.date = self._de_date.date().toString("yyyy-MM-dd")
            else:
                self.entry.repeat = "daily"
                self.entry.days = []
        self.accept()


# ============================================================ メイン編集ウィンドウ
class FlowEditorWindow(QWidget):
    """週間スケジュールテーブル形式のフロー編集ウィンドウ。"""

    SLOTS = 48      # 00:00〜23:30 を 30 分刻みで 48 行
    saved   = Signal(str)
    applied = Signal(str)   # 「保存して反映」: メインで即時 reload を依頼
    closed  = Signal(object)

    def __init__(self, flow_path: str | None) -> None:
        super().__init__(None)
        self.setWindowTitle("フロー編集")
        self.resize(1100, 800)

        if flow_path and os.path.exists(flow_path):
            try:
                self._flow = load_pc_flow(flow_path)
            except Exception:
                self._flow = PcFlow(name=os.path.splitext(os.path.basename(flow_path))[0])
            self._path: str | None = flow_path
        else:
            stem = os.path.splitext(os.path.basename(flow_path or "新規フロー"))[0]
            self._flow = PcFlow(name=stem)
            self._path = flow_path

        self._build_ui()
        self._refresh_table()

        # 起動しっぱなしでの日付跨ぎ + 現在時刻の赤線移動に対応するため
        # 1 秒ごとに描画更新、日付の曜日変化を検出して曜日列着色を更新する
        self._last_today_wd = datetime.now().weekday()
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start(1000)

    def _on_tick(self) -> None:
        # 赤線位置の再描画
        self._table.viewport().update()
        # 日付跨ぎで「今日の曜日」が変わったら曜日着色を全更新
        new_wd = datetime.now().weekday()
        if new_wd != self._last_today_wd:
            self._last_today_wd = new_wd
            self._refresh_table()

    # ----------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # ヘッダー
        head = QHBoxLayout()
        head.addWidget(QLabel("フロー名:"))
        self._inp_name = QLineEdit(self._flow.name)
        head.addWidget(self._inp_name, 1)
        head.addStretch(1)
        btn_save = QPushButton("保存")
        btn_save.setFixedWidth(80)
        btn_save.setToolTip("ファイルに保存のみ。実行中ランナーには反映されません")
        btn_save.clicked.connect(self._save)
        head.addWidget(btn_save)
        btn_save_apply = QPushButton("保存して反映")
        btn_save_apply.setFixedWidth(120)
        btn_save_apply.setToolTip(
            "保存後、メインで選択中のフローと同じならランナーへ即時反映します。\n"
            "実行中なら次のスケジュールチェックから新しい定義で動きます。"
        )
        btn_save_apply.setStyleSheet("font-weight:bold;")
        btn_save_apply.clicked.connect(self._save_and_apply)
        head.addWidget(btn_save_apply)
        outer.addLayout(head)

        # テーブル（QTableWidget サブクラス: 現在時刻の赤線を描画）
        self._table = _ScheduleTable(self.SLOTS, 7)
        self._table.setHorizontalHeaderLabels(DAY_NAMES)
        times = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]
        self._table.setVerticalHeaderLabels(times)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectItems)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        # 曜日列を均等幅に
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Stretch)
        # 行は少しコンパクトに
        for r in range(self.SLOTS):
            self._table.setRowHeight(r, 22)
        # 今日の曜日列を強調（ヘッダー太字）— 実際の色付けは _refresh_table で
        self._apply_today_header_bold()
        outer.addWidget(self._table, 1)

        # 下部ボタン
        bot = QHBoxLayout()
        btn_new = QPushButton("+ 新規エントリ")
        btn_new.clicked.connect(lambda: self._add_entry())
        btn_edit = QPushButton("選択を編集…")
        btn_edit.clicked.connect(self._edit_selected)
        btn_del = QPushButton("選択を削除")
        btn_del.clicked.connect(self._delete_selected)
        bot.addWidget(btn_new)
        bot.addWidget(btn_edit)
        bot.addWidget(btn_del)
        bot.addStretch(1)
        outer.addLayout(bot)

        hint = QLabel(
            "操作: 空セルをダブルクリック=新規 / 既存セルをダブルクリック=編集"
            " / セル選択して下のボタンで操作"
        )
        hint.setStyleSheet("color:#666; font-size:11px;")
        outer.addWidget(hint)

        # ---- 続けて実行（seq エントリ）パネル
        seq_head = QHBoxLayout()
        seq_head.addWidget(QLabel("続けて実行（時刻指定エントリ完了後に連鎖実行）:"))
        seq_head.addStretch(1)
        btn_seq_new = QPushButton("+ 続けて実行追加")
        btn_seq_new.setToolTip(
            "選択中の時刻エントリの直後に「続けて実行」エントリを追加します。\n"
            "テーブルでセルを選択してから押してください。"
        )
        btn_seq_new.clicked.connect(self._add_seq_entry)
        seq_head.addWidget(btn_seq_new)
        btn_seq_edit = QPushButton("編集…")
        btn_seq_edit.clicked.connect(self._edit_selected_seq)
        seq_head.addWidget(btn_seq_edit)
        btn_seq_del = QPushButton("削除")
        btn_seq_del.clicked.connect(self._delete_selected_seq)
        seq_head.addWidget(btn_seq_del)
        outer.addLayout(seq_head)

        self._seq_list = QListWidget()
        self._seq_list.setStyleSheet("font-size:12px;")
        self._seq_list.setMaximumHeight(140)
        self._seq_list.itemDoubleClicked.connect(
            lambda *_: self._edit_selected_seq()
        )
        # DEL キーで削除
        sc_del = QShortcut(QKeySequence(Qt.Key_Delete), self._seq_list)
        sc_del.setContext(Qt.WidgetShortcut)
        sc_del.activated.connect(self._delete_selected_seq)
        outer.addWidget(self._seq_list)

    # ------------------------------------------------------- テーブル描画
    _BG_TODAY = QColor("#fff9c4")    # 今日の曜日列の背景（薄黄）

    def _apply_today_header_bold(self) -> None:
        """ヘッダーで今日の曜日だけ太字にする。"""
        today_wd = datetime.now().weekday()
        for c in range(self._table.columnCount()):
            hdr = self._table.horizontalHeaderItem(c)
            if hdr is None:
                continue
            f = hdr.font()
            f.setBold(c == today_wd)
            hdr.setFont(f)

    def _refresh_table(self) -> None:
        self._table.clearContents()
        today_wd = datetime.now().weekday()
        today_bg = QBrush(self._BG_TODAY)
        # まず今日列に空のセルを置いて背景色を付ける（あとでエントリで上書きされる）
        for r in range(self.SLOTS):
            item = QTableWidgetItem("")
            item.setBackground(today_bg)
            self._table.setItem(r, today_wd, item)
        # エントリを各曜日に展開（seq エントリはテーブル外のリストに表示）
        for entry in self._flow.schedule:
            if entry.seq:
                continue
            row = self._time_to_row(entry.time)
            if row is None:
                continue
            for col in self._entry_columns(entry):
                exist = self._table.item(row, col)
                text = self._entry_short_label(entry)
                if exist is None or not exist.text():
                    item = QTableWidgetItem(text)
                else:
                    item = QTableWidgetItem(exist.text() + "\n" + text)
                if not entry.enabled:
                    item.setForeground(QBrush(QColor("#aaa")))
                else:
                    if entry.repeat == "daily":
                        item.setForeground(QBrush(QColor("#1565c0")))
                    elif entry.repeat == "weekly":
                        item.setForeground(QBrush(QColor("#2e7d32")))
                    elif entry.repeat == "once":
                        item.setForeground(QBrush(QColor("#ef6c00")))
                if col == today_wd:
                    item.setBackground(today_bg)
                item.setToolTip(self._entry_tooltip(entry))
                self._table.setItem(row, col, item)
        # ヘッダー強調も更新（日付跨ぎ時に正しい列が太字になるよう）
        self._apply_today_header_bold()
        # 続けて実行リストも併せて更新
        if hasattr(self, "_seq_list"):
            self._refresh_seq_list()

    @staticmethod
    def _time_to_row(t: str) -> int | None:
        try:
            h, m = t.split(":")
            return int(h) * 2 + (0 if int(m) < 30 else 1)
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _row_to_time(row: int) -> str:
        h = row // 2
        m = (row % 2) * 30
        return f"{h:02d}:{m:02d}"

    @staticmethod
    def _entry_columns(entry: ScheduleEntry) -> list[int]:
        if entry.repeat == "daily":
            return list(range(7))
        if entry.repeat == "weekly":
            return list(entry.days) if entry.days else list(range(7))
        if entry.repeat == "once":
            try:
                d = datetime.strptime(entry.date, "%Y-%m-%d").date()
                return [d.weekday()]
            except (ValueError, TypeError):
                return []
        return []

    @staticmethod
    def _entry_short_label(entry: ScheduleEntry) -> str:
        scenes = entry_scenes(entry)
        name = os.path.splitext(scenes[0])[0] if scenes else (entry.target or "(未設定)")
        return f"{entry.time} {name}"

    @staticmethod
    def _entry_tooltip(entry: ScheduleEntry) -> str:
        scenes = entry_scenes(entry)
        if entry.repeat == "weekly":
            rep = "週次 " + "・".join(DAY_NAMES[d] for d in entry.days)
        elif entry.repeat == "once":
            rep = f"1 回限り {entry.date}"
        else:
            rep = "毎日"
        return f"{entry.time}  {rep}\nシーン: {', '.join(scenes) or '(未設定)'}"

    # ------------------------------------------------------- セル操作
    def _on_cell_double_clicked(self, row: int, col: int) -> None:
        time_str = self._row_to_time(row)
        match = self._find_entry_at(time_str, col)
        if match is None:
            self._add_entry(initial_time=time_str, initial_weekday=col)
        else:
            self._edit_entry(match)

    def _find_entry_at(self, time_str: str, weekday: int) -> ScheduleEntry | None:
        """time_str が示す 30 分スロット内のエントリを返す。

        time_str は _row_to_time によって `HH:00` または `HH:30` のいずれか
        （行スロットの基準時刻）。エントリ自身の time が 05:10 のような中途半端な
        値でも、行スロットが同じなら一致と判定する（厳密一致だと削除/編集できない）。
        """
        target_row = self._time_to_row(time_str)
        if target_row is None:
            return None
        for entry in self._flow.schedule:
            if entry.seq:
                continue   # 続けて実行はグリッド対象外
            if self._time_to_row(entry.time) != target_row:
                continue
            if weekday not in self._entry_columns(entry):
                continue
            return entry
        return None

    def _selected_cell(self) -> tuple[int, int] | None:
        items = self._table.selectedItems()
        if not items:
            return None
        it = items[0]
        return it.row(), it.column()

    def _edit_selected(self) -> None:
        cell = self._selected_cell()
        if cell is None:
            return
        row, col = cell
        match = self._find_entry_at(self._row_to_time(row), col)
        if match is None:
            self._add_entry(initial_time=self._row_to_time(row), initial_weekday=col)
        else:
            self._edit_entry(match)

    def _delete_selected(self) -> None:
        cell = self._selected_cell()
        if cell is None:
            return
        row, col = cell
        match = self._find_entry_at(self._row_to_time(row), col)
        if match is None:
            return
        if QMessageBox.question(
            self, "削除確認",
            f"{match.time} のエントリを削除しますか？",
        ) != QMessageBox.Yes:
            return
        self._flow.schedule.remove(match)
        self._refresh_table()

    # ------------------------------------------------------- 続けて実行リスト
    def _refresh_seq_list(self) -> None:
        """schedule リスト中の seq エントリを表示し、直前の親エントリを併記する。"""
        self._seq_list.clear()
        parent_label = "(直前なし — 単独では実行されません)"
        for idx, entry in enumerate(self._flow.schedule):
            if not entry.seq:
                scenes = entry_scenes(entry)
                name = (
                    os.path.splitext(scenes[0])[0]
                    if scenes else (entry.target or "(未設定)")
                )
                if entry.repeat == "weekly":
                    when = "週次 " + (
                        "・".join(DAY_NAMES[d] for d in entry.days)
                        if entry.days else "毎日"
                    )
                elif entry.repeat == "once":
                    when = f"1 回限り {entry.date}"
                else:
                    when = "毎日"
                parent_label = f"{entry.time} {when}: {name}"
                continue
            # seq エントリ
            scenes = entry_scenes(entry)
            scene_name = (
                os.path.splitext(scenes[0])[0]
                if scenes else (entry.target or "(未設定)")
            )
            mark = "→" if entry.enabled else "✗"
            text = f"{mark} {scene_name}    (← {parent_label} の続き)"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, idx)
            if not entry.enabled:
                item.setForeground(QBrush(QColor("#aaa")))
            self._seq_list.addItem(item)

    def _selected_seq_entry(self) -> tuple[int, ScheduleEntry] | None:
        row = self._seq_list.currentRow()
        if row < 0:
            return None
        idx = self._seq_list.item(row).data(Qt.UserRole)
        if idx is None or idx >= len(self._flow.schedule):
            return None
        entry = self._flow.schedule[idx]
        if not entry.seq:
            return None
        return idx, entry

    def _add_seq_entry(self) -> None:
        cell = self._selected_cell()
        if cell is None:
            QMessageBox.information(
                self, "続けて実行追加",
                "テーブルで親となる時刻エントリのセルを選択してから押してください",
            )
            return
        row, col = cell
        parent = self._find_entry_at(self._row_to_time(row), col)
        if parent is None:
            QMessageBox.information(
                self, "続けて実行追加",
                "選択中のセルにエントリがありません",
            )
            return
        # 親の直後に並ぶ seq エントリ群の末尾に挿入
        parent_idx = self._flow.schedule.index(parent)
        insert_idx = parent_idx + 1
        while (
            insert_idx < len(self._flow.schedule)
            and self._flow.schedule[insert_idx].seq
        ):
            insert_idx += 1
        scenes_list = self._list_scenes()
        new_entry = ScheduleEntry(
            target=scenes_list[0] if scenes_list else "",
            seq=True,
            enabled=True,
        )
        dlg = _EntryDialog(new_entry, scenes_list, self, mode="seq")
        if dlg.exec():
            self._flow.schedule.insert(insert_idx, dlg.entry)
            self._refresh_table()

    def _edit_selected_seq(self) -> None:
        sel = self._selected_seq_entry()
        if sel is None:
            return
        _, entry = sel
        dlg = _EntryDialog(entry, self._list_scenes(), self, mode="seq")
        if dlg.exec():
            self._refresh_table()

    def _delete_selected_seq(self) -> None:
        sel = self._selected_seq_entry()
        if sel is None:
            return
        idx, entry = sel
        scenes = entry_scenes(entry)
        name = (
            os.path.splitext(scenes[0])[0]
            if scenes else (entry.target or "(未設定)")
        )
        if QMessageBox.question(
            self, "削除確認",
            f"続けて実行エントリ「{name}」を削除しますか？",
        ) != QMessageBox.Yes:
            return
        del self._flow.schedule[idx]
        self._refresh_table()

    # ------------------------------------------------------- エントリ追加 / 編集
    def _list_scenes(self) -> list[str]:
        """フロー候補（flow_target=True）のシーンのみ返す。

        load_pc_scene のパースに失敗したファイルや読めないものは候補に出す
        （誤って隠してしまうより、ユーザーが気付けるほうが良い）。
        '_' で始まるファイル（並び順 _order.json など内部用）は除外する。
        """
        if not os.path.isdir(SCENES_DIR):
            return []
        out: list[str] = []
        for fname in sorted(os.listdir(SCENES_DIR)):
            if not fname.endswith(".json") or fname.startswith("_"):
                continue
            path = os.path.join(SCENES_DIR, fname)
            try:
                scene = load_pc_scene(path)
                if not scene.flow_target:
                    continue
            except Exception:
                # 読めなくても一覧には出す（隠すと存在自体を忘れる）
                pass
            out.append(fname)
        return out

    def _add_entry(
        self,
        initial_time: str | None = None,
        initial_weekday: int | None = None,
    ) -> None:
        scenes_list = self._list_scenes()
        new_entry = ScheduleEntry(
            time=initial_time or "12:00",
            target=scenes_list[0] if scenes_list else "",
            repeat="weekly" if initial_weekday is not None else "daily",
            days=[initial_weekday] if initial_weekday is not None else [],
            enabled=True,
        )
        dlg = _EntryDialog(new_entry, scenes_list, self)
        if dlg.exec():
            self._flow.schedule.append(dlg.entry)
            self._refresh_table()

    def _edit_entry(self, entry: ScheduleEntry) -> None:
        dlg = _EntryDialog(entry, self._list_scenes(), self)
        if dlg.exec():
            self._refresh_table()

    # ------------------------------------------------------- 保存
    def _save_to_file(self) -> bool:
        """保存本体。成功で True、失敗で False。saved シグナルも発火する。"""
        name = self._inp_name.text().strip()
        if name:
            self._flow.name = name
        if not self._flow.name:
            QMessageBox.warning(self, "エラー", "フロー名を入力してください")
            return False
        if self._path is None:
            self._path = os.path.join(FLOWS_DIR, f"{self._flow.name}.json")
        try:
            save_pc_flow(self._flow, self._path)
        except Exception as e:
            QMessageBox.warning(self, "保存失敗", str(e))
            return False
        self.saved.emit(self._path)
        return True

    def _save(self) -> None:
        if not self._save_to_file():
            return
        QMessageBox.information(self, "保存", f"保存しました: {self._path}")

    def _save_and_apply(self) -> None:
        """保存後、メインに即時反映を依頼する。"""
        if not self._save_to_file():
            return
        self.applied.emit(self._path)
        # 反映の結果（実行中か否か）はメイン側のログに出る。ここではダイアログは控えめに
        QMessageBox.information(
            self, "保存して反映",
            f"保存しました: {self._path}\n"
            "メインで選択中のフローと同じなら、ランナーへ反映済みです。",
        )

    def closeEvent(self, e) -> None:  # noqa: N802
        try:
            self._tick_timer.stop()
        except Exception:
            pass
        self.closed.emit(self)
        super().closeEvent(e)
