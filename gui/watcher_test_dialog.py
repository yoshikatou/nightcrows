"""ウォッチャー一括テストダイアログ。

スクショ or 画像ファイル1枚に対して、登録済みウォッチャーすべての
マッチスコアを計算して並べる。誤発火（似た画像で別ウォッチャーが当たる）
の検証用。

判定ロジックは flow_runner._evaluate_condition と一致させるが、
こちらは hit/miss だけでなく **生スコアとマージン** を返す。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np
from PySide6.QtCore import Qt, QRect, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget, QHeaderView,
)

from .adb import screencap
from .flow import Condition, Watcher
from .ocr_test_dialog import ImageCanvas

# 領域オーバーレイ色（ウォッチャーごとに循環）
_OVERLAY_COLORS = [
    "#e53935", "#fb8c00", "#fdd835", "#43a047",
    "#039be5", "#3949ab", "#8e24aa", "#00897b",
    "#6d4c41", "#546e7a",
]


@dataclass
class TestResult:
    """1ウォッチャーぶんのテスト結果。"""
    watcher: Watcher
    color: str = "#888"
    hit: bool = False
    score: float | None = None         # image系: 0.0-1.0  /  OCR/数値系: None
    margin: float | None = None        # image系のみ score - threshold
    read_value: int | None = None      # ocr_number / digit_threshold で読めた値
    match_loc: tuple[int, int, int, int] | None = None  # ヒット時の画像座標 (x,y,w,h)
    note: str = ""
    error: str = ""


# ============================================================ スコア計算
def _image_match_score(c: Condition, img: np.ndarray
                       ) -> tuple[float | None, tuple[int, int, int, int] | None, str]:
    """region 内でテンプレマッチを行い (max_score, match_loc, error) を返す。

    match_loc は画像全体座標での (x, y, w, h)。テンプレが読めない等で
    判定不能なら (None, None, error_msg)。
    """
    if not c.template:
        return None, None, "template 未設定"
    tmpl = cv2.imread(c.template, cv2.IMREAD_COLOR)
    if tmpl is None:
        return None, None, f"テンプレ画像読込失敗: {c.template}"

    rx, ry = 0, 0
    target = img
    if c.region and len(c.region) == 4:
        x, y, w, h = c.region
        h_img, w_img = img.shape[:2]
        x2 = min(x + w, w_img)
        y2 = min(y + h, h_img)
        x = max(0, x); y = max(0, y)
        target = img[y:y2, x:x2]
        rx, ry = x, y

    if target.size == 0:
        return None, None, "region が画像範囲外"
    if target.shape[0] < tmpl.shape[0] or target.shape[1] < tmpl.shape[1]:
        return None, None, "region がテンプレより小さい"

    res = cv2.matchTemplate(target, tmpl, cv2.TM_CCOEFF_NORMED)
    _, maxv, _, maxloc = cv2.minMaxLoc(res)
    th, tw = tmpl.shape[:2]
    return float(maxv), (rx + maxloc[0], ry + maxloc[1], tw, th), ""


def _read_ocr_number(c: Condition, img: np.ndarray) -> tuple[int | None, str]:
    """flow_runner._ocr_number と同じ前処理で値を読み取る。"""
    try:
        import pytesseract
    except ImportError:
        return None, "pytesseract 未インストール"
    if not c.region or len(c.region) != 4:
        return None, "region 未設定"
    x, y, w, h = c.region
    h_img, w_img = img.shape[:2]
    x2 = min(x + w, w_img); y2 = min(y + h, h_img)
    crop = img[max(0, y):y2, max(0, x):x2]
    if crop.size == 0:
        return None, "region が画像範囲外"
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    _, gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    config = "--psm 7 --oem 3"
    wl = (c.ocr_whitelist or "").strip()
    if wl:
        config += f" -c tessedit_char_whitelist={wl}"
    try:
        text = pytesseract.image_to_string(gray, config=config).strip()
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            return None, f"数値読取失敗（生テキスト: {text!r}）"
        return int(digits), ""
    except Exception as e:
        return None, f"OCR 例外: {e}"


def _read_digit_template(c: Condition, img: np.ndarray) -> tuple[int | None, str]:
    """digit_threshold 用 — 0.png〜9.png でテンプレマッチ。

    flow_runner._read_digits と同じロジック。
    """
    if not c.digits_dir:
        return None, "digits_dir 未設定"
    if not c.region or len(c.region) != 4:
        return None, "region 未設定"
    x, y, w, h = c.region
    h_img, w_img = img.shape[:2]
    x2 = min(x + w, w_img); y2 = min(y + h, h_img)
    target = img[max(0, y):y2, max(0, x):x2]
    if target.size == 0:
        return None, "region が画像範囲外"

    matches: list[tuple[int, int, float]] = []
    min_tmpl_w = 999
    for d in range(10):
        path = os.path.join(c.digits_dir, f"{d}.png")
        tmpl = cv2.imread(path, cv2.IMREAD_COLOR)
        if tmpl is None:
            continue
        if target.shape[0] < tmpl.shape[0] or target.shape[1] < tmpl.shape[1]:
            continue
        min_tmpl_w = min(min_tmpl_w, tmpl.shape[1])
        res = cv2.matchTemplate(target, tmpl, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= 0.8)
        for yi, xi in zip(ys, xs):
            matches.append((int(xi), d, float(res[yi, xi])))
    if not matches:
        return None, "数字が読み取れません"
    matches.sort(key=lambda m: m[0])
    min_sep = max(4, min_tmpl_w // 2) if min_tmpl_w != 999 else 8
    filtered: list[tuple[int, int, float]] = []
    for m in matches:
        if filtered and m[0] - filtered[-1][0] < min_sep:
            if m[2] > filtered[-1][2]:
                filtered[-1] = m
            continue
        filtered.append(m)
    if not filtered:
        return None, "数字が読み取れません"
    try:
        return int("".join(str(m[1]) for m in filtered)), ""
    except ValueError:
        return None, "数字パース失敗"


def _compare(a: int, op: str, b: int) -> bool:
    return {
        "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a == b,
    }.get(op, False)


def _test_watcher(w: Watcher, img: np.ndarray) -> TestResult:
    """1ウォッチャーを 1 枚の画像に対してテストする。"""
    r = TestResult(watcher=w)
    c = w.condition

    if c.type in ("image_appear", "image_gone"):
        score, loc, err = _image_match_score(c, img)
        if err:
            r.error = err
            return r
        r.score = score
        r.margin = score - c.threshold
        if c.type == "image_appear":
            r.hit = score >= c.threshold
            r.match_loc = loc if r.hit else None
            r.note = "テンプレが見つかれば発火"
        else:  # image_gone
            visible = score >= c.threshold
            # 単発で「いま見えていない」なら発火予備状態。本番は consecutive 回必要
            r.hit = not visible
            r.match_loc = loc if visible else None
            r.note = (
                f"⚠️ 単発判定: いま{'見えてる' if visible else '見えてない'}。"
                f"本番は {c.consecutive} 回連続消失で発火"
            )
        return r

    if c.type == "ocr_number":
        val, err = _read_ocr_number(c, img)
        if err and val is None:
            r.error = err
            return r
        r.read_value = val
        r.hit = _compare(val, c.op, c.value)
        r.note = f"読取値 {val} {c.op} {c.value} → {'真' if r.hit else '偽'}"
        if c.consecutive > 1:
            r.note += f"  (本番は {c.consecutive} 回連続で発火)"
        return r

    if c.type == "digit_threshold":
        val, err = _read_digit_template(c, img)
        if err and val is None:
            r.error = err
            return r
        r.read_value = val
        r.hit = _compare(val, c.op, c.value)
        r.note = f"読取値 {val} {c.op} {c.value} → {'真' if r.hit else '偽'}"
        if c.consecutive > 1:
            r.note += f"  (本番は {c.consecutive} 回連続で発火)"
        return r

    r.error = f"未対応の condition.type: {c.type}"
    return r


# ============================================================ オーバーレイ付きキャンバス
class _OverlayCanvas(ImageCanvas):
    """ImageCanvas に複数の領域オーバーレイ描画を追加。"""

    def __init__(self) -> None:
        super().__init__()
        # [(rect_xywh, color_hex, label, is_match_box)]
        # is_match_box=False は region 枠、True はマッチ位置の小枠
        self._overlays: list[tuple[tuple[int, int, int, int], str, str, bool]] = []
        self._highlight_idx: int = -1   # ハイライト中のオーバーレイインデックス

    def set_overlays(self,
                     items: list[tuple[tuple[int, int, int, int], str, str, bool]]
                     ) -> None:
        self._overlays = list(items)
        self._highlight_idx = -1
        self.update()

    def highlight_overlay(self, idx: int) -> None:
        self._highlight_idx = idx
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._overlays or not self._pixmap:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        for i, ((x, y, w, h), color, label, is_match) in enumerate(self._overlays):
            r = self._img_to_widget(QRect(x, y, w, h))
            qc = QColor(color)
            is_hl = (i == self._highlight_idx)
            width = 4 if is_hl else (2 if not is_match else 2)
            style = Qt.SolidLine if is_match else Qt.DashLine
            p.setPen(QPen(qc, width, style))
            p.setBrush(Qt.NoBrush)
            p.drawRect(r)
            # ラベル背景
            if label:
                font = QFont(); font.setPointSize(8); font.setBold(is_hl)
                p.setFont(font)
                fm = p.fontMetrics()
                tw = fm.horizontalAdvance(label) + 6
                th = fm.height() + 2
                lx = max(r.left(), 0)
                ly = max(r.top() - th, 0)
                bg = QColor(qc); bg.setAlpha(220)
                p.fillRect(QRect(lx, ly, tw, th), bg)
                p.setPen(QColor("#ffffff"))
                p.drawText(lx + 3, ly + fm.ascent() + 1, label)
        p.end()


# ============================================================ ダイアログ本体
class WatcherTestDialog(QDialog):
    """登録済みウォッチャーすべてに 1 枚の画像をぶつけるテストダイアログ。"""

    def __init__(self, watchers: list[Watcher],
                 serial: str | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("🧪 ウォッチャー一括テスト — 誤発火検証")
        self.setMinimumSize(1100, 720)
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowMaximizeButtonHint | Qt.WindowMinimizeButtonHint
        )

        self._serial = serial
        self._watchers = list(watchers)
        self._img: np.ndarray | None = None
        self._results: list[TestResult] = []

        self._build_ui()

    # ----------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # 上段: 画像取得ボタン
        top = QHBoxLayout()
        btn_cap = QPushButton("📷 スクショ取得")
        btn_cap.clicked.connect(self._capture)
        btn_file = QPushButton("📂 ファイルから開く")
        btn_file.clicked.connect(self._open_file)
        top.addWidget(btn_cap)
        top.addWidget(btn_file)
        top.addSpacing(20)
        self.chk_disabled = QCheckBox("無効ウォッチャーも表示")
        self.chk_disabled.setChecked(False)
        self.chk_disabled.toggled.connect(self._rerun)
        top.addWidget(self.chk_disabled)
        self.chk_show_all_regions = QCheckBox("全ウォッチャーの領域を表示")
        self.chk_show_all_regions.setChecked(True)
        self.chk_show_all_regions.toggled.connect(self._refresh_overlays)
        top.addWidget(self.chk_show_all_regions)
        top.addStretch()
        self.lbl_summary = QLabel("画像を読み込んでください")
        self.lbl_summary.setStyleSheet("color:#555; font-size:11px;")
        top.addWidget(self.lbl_summary)
        root.addLayout(top)

        # 中段: スプリッタ（左:キャンバス  右:結果テーブル）
        sp = QSplitter(Qt.Horizontal)

        self._canvas = _OverlayCanvas()
        sp.addWidget(self._canvas)

        right = QWidget()
        rl = QVBoxLayout(right); rl.setContentsMargins(0, 0, 0, 0)

        rl.addWidget(QLabel(
            "スコアの高い順。✅ がしきい値超え（発火）。複数 ✅ なら "
            "👑 のついたものが本番の勝者（priority 大→配列順）"
        ))
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels([
            "判定", "ウォッチャー", "種別", "スコア / 値", "しきい値", "マージン", "備考"
        ])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        self.table.currentCellChanged.connect(self._on_row_changed)
        rl.addWidget(self.table, 1)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(140)
        rl.addWidget(self.detail)

        sp.addWidget(right)
        sp.setStretchFactor(0, 3)
        sp.setStretchFactor(1, 4)
        root.addWidget(sp, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    # ----------------------------------------------------------- 画像取得
    def _capture(self) -> None:
        if not self._serial:
            QMessageBox.information(
                self, "情報",
                "デバイス未接続です。先にメインウィンドウで接続してください。"
            )
            return
        try:
            png = screencap(self._serial)
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"スクショ失敗: {e}")
            return
        arr = np.frombuffer(png, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            QMessageBox.critical(self, "エラー", "PNG デコード失敗")
            return
        self._set_image(img)

    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "画像を開く", "templates/snapshots",
            "Images (*.png *.jpg *.jpeg *.bmp)"
        )
        if not path:
            return
        # 日本語パス対策で fromfile → imdecode
        try:
            data = np.fromfile(path, dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"画像読込失敗: {e}")
            return
        if img is None:
            QMessageBox.critical(self, "エラー", "画像が読めません")
            return
        self._set_image(img)

    def _set_image(self, img: np.ndarray) -> None:
        self._img = img
        self._canvas.set_image(img)
        self._rerun()

    # ----------------------------------------------------------- テスト実行
    def _rerun(self) -> None:
        if self._img is None:
            return
        targets = self._watchers if self.chk_disabled.isChecked() \
            else [w for w in self._watchers if w.enabled]

        self._results = []
        for i, w in enumerate(targets):
            r = _test_watcher(w, self._img)
            r.color = _OVERLAY_COLORS[i % len(_OVERLAY_COLORS)]
            self._results.append(r)

        # 並べ替え: ヒット優先 → スコア降順 → エラーは末尾
        def sort_key(r: TestResult):
            err = 1 if r.error else 0
            hit = 0 if r.hit else 1
            score = -(r.score if r.score is not None else -1.0)
            return (err, hit, score)
        self._results.sort(key=sort_key)

        # 本番の勝者（hit かつ enabled の中で priority 最大、同値は配列順）
        winner_id = self._compute_winner_id()

        self._populate_table(winner_id)
        self._refresh_overlays()
        self._update_summary(winner_id)

    def _compute_winner_id(self) -> str | None:
        candidates = [
            (i, r) for i, r in enumerate(self._results)
            if r.hit and r.watcher.enabled
        ]
        if not candidates:
            return None
        # 元のウォッチャー配列順をタイブレーカに使う
        order_index = {w.id: idx for idx, w in enumerate(self._watchers)}
        candidates.sort(
            key=lambda ir: (-ir[1].watcher.priority,
                            order_index.get(ir[1].watcher.id, 999))
        )
        return candidates[0][1].watcher.id

    def _populate_table(self, winner_id: str | None) -> None:
        self.table.setRowCount(len(self._results))
        for row, r in enumerate(self._results):
            w = r.watcher
            # 判定セル
            if r.error:
                judge = "⚠️ ERR"
                color = QColor("#bdbdbd")
            elif r.hit and w.id == winner_id:
                judge = "👑 HIT"
                color = QColor("#fff59d")
            elif r.hit:
                judge = "✅ HIT"
                color = QColor("#c8e6c9")
            else:
                judge = "❌"
                color = None

            cells = [
                judge,
                f"●  {w.title or w.id}" + ("" if w.enabled else "  (無効)"),
                w.condition.type,
                self._fmt_score(r),
                self._fmt_threshold(r),
                self._fmt_margin(r),
                r.note or r.error or "",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 1:
                    # ウォッチャー名セルに色マーカー（前景色を overlay 色に）
                    item.setForeground(QBrush(QColor(r.color)))
                    f = QFont(); f.setBold(True); item.setFont(f)
                if color is not None:
                    item.setBackground(QBrush(color))
                if not w.enabled:
                    item.setForeground(QBrush(QColor("#888")))
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        if self.table.rowCount() > 0:
            self.table.setCurrentCell(0, 0)

    def _fmt_score(self, r: TestResult) -> str:
        if r.score is not None:
            return f"{r.score:.3f}"
        if r.read_value is not None:
            return str(r.read_value)
        return "—"

    def _fmt_threshold(self, r: TestResult) -> str:
        c = r.watcher.condition
        if c.type in ("image_appear", "image_gone"):
            return f"{c.threshold:.3f}"
        if c.type in ("ocr_number", "digit_threshold"):
            return f"{c.op} {c.value}"
        return "—"

    def _fmt_margin(self, r: TestResult) -> str:
        if r.margin is None:
            return "—"
        sign = "+" if r.margin >= 0 else ""
        return f"{sign}{r.margin:.3f}"

    def _refresh_overlays(self) -> None:
        items: list[tuple[tuple[int, int, int, int], str, str, bool]] = []
        show_all = self.chk_show_all_regions.isChecked()
        for r in self._results:
            c = r.watcher.condition
            if not c.region or len(c.region) != 4:
                continue
            if not show_all and not r.hit:
                continue
            x, y, w, h = c.region
            label = r.watcher.title or r.watcher.id
            items.append(((x, y, w, h), r.color, label, False))
            # ヒット時はマッチ位置の枠も足す
            if r.hit and r.match_loc:
                items.append((r.match_loc, r.color, "", True))
        self._canvas.set_overlays(items)

    def _update_summary(self, winner_id: str | None) -> None:
        hits = [r for r in self._results if r.hit]
        errs = [r for r in self._results if r.error]
        parts = [f"対象 {len(self._results)} 件"]
        parts.append(f"ヒット {len(hits)}")
        if errs:
            parts.append(f"エラー {len(errs)}")
        if winner_id:
            winner = next((r.watcher for r in self._results if r.watcher.id == winner_id), None)
            if winner:
                parts.append(f"勝者: 👑 {winner.title or winner.id} (priority={winner.priority})")
        if len(hits) > 1:
            parts.append("⚠️ 複数ヒット — 誤発火の可能性")
        elif len(hits) == 1:
            parts.append("✅ 単独ヒット")
        else:
            parts.append("❌ 該当なし")
        self.lbl_summary.setText("  /  ".join(parts))

    # ----------------------------------------------------------- 詳細 & ハイライト
    def _on_row_changed(self, row: int, _col: int, _pr: int, _pc: int) -> None:
        if row < 0 or row >= len(self._results):
            self.detail.clear()
            self._canvas.highlight_overlay(-1)
            return
        r = self._results[row]
        # キャンバスのオーバーレイ idx を計算（_refresh_overlays と同じ順序）
        overlay_idx = -1
        idx = 0
        show_all = self.chk_show_all_regions.isChecked()
        for rr in self._results:
            c = rr.watcher.condition
            if not c.region or len(c.region) != 4:
                continue
            if not show_all and not rr.hit:
                continue
            if rr.watcher.id == r.watcher.id:
                overlay_idx = idx
                break
            idx += 1
            if rr.hit and rr.match_loc:
                idx += 1
        self._canvas.highlight_overlay(overlay_idx)

        w = r.watcher; c = w.condition
        lines: list[str] = []
        lines.append(f"<b>{w.title or w.id}</b>  (id={w.id})")
        lines.append(f"種別: {c.type}  /  priority={w.priority}  /  enabled={w.enabled}")
        if c.type in ("image_appear", "image_gone"):
            lines.append(f"テンプレ: {c.template}")
            lines.append(f"region: {c.region}    threshold: {c.threshold:.3f}")
            if r.score is not None:
                lines.append(f"スコア: <b>{r.score:.4f}</b>   "
                             f"マージン: <b>{r.margin:+.4f}</b>")
            if r.match_loc:
                lines.append(f"マッチ位置: x={r.match_loc[0]} y={r.match_loc[1]} "
                             f"w={r.match_loc[2]} h={r.match_loc[3]}")
        else:
            lines.append(f"region: {c.region}    判定: {c.op} {c.value}")
            if r.read_value is not None:
                lines.append(f"読取値: <b>{r.read_value}</b>")
        if r.note:
            lines.append(f"<i>{r.note}</i>")
        if r.error:
            lines.append(f"<span style='color:#c00'>エラー: {r.error}</span>")
        lines.append(f"handler: {w.handler}    after: {w.after}")
        self.detail.setHtml("<br>".join(lines))
