"""PC フロースケジューラー。

mobile/gui/flow.py と同じ JSON フォーマット（schedule・settings）を扱う。
mobile/ への依存を持たず PC 単体で動作するよう、必要な部分を再実装している。

実行ループ:
    1. スケジュール発火を検出 → 対応するシーン列を順次実行
    2. シーン実行中も別スレッドでウォッチャーをポーリング
    3. ウォッチャー発火 → シーン中断 → handler シーン実行 → after 動作
       - after = "restart_scene": 同じシーンを最初からやり直す
       - after = "next_scene":    次のシーンへ進む
       - after = "stop":          フロー停止
       - after = "noop":          何もしない（中断したシーンはスキップ）
"""
from __future__ import annotations

import ctypes
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

import win32con
import win32gui
from PySide6.QtCore import QObject, Signal

from .capture import capture_window
from .logger import write_log
from .notify import send_google_chat
from .pc_scene import SCENES_DIR, load_pc_scene, run_pc_scene
from .pc_watcher import PcWatcher, evaluate_watcher, list_pc_watchers
from .watcher_counts import record_fire as record_watcher_fire
from .window_picker import find_hwnd_by_title


# ----------------------------------------------------------------- 前面化ユーティリティ
def is_window_foreground(hwnd: int) -> bool:
    """対象ウィンドウが最前面でフォーカスされているか。

    最小化中は False。ウィンドウハンドル無効も False。
    """
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False
    if win32gui.IsIconic(hwnd):
        return False
    return win32gui.GetForegroundWindow() == hwnd


def bring_window_to_foreground(hwnd: int) -> bool:
    """対象ウィンドウを前面化する。成功なら True。

    最小化解除 → SwitchToThisWindow による Alt+Tab 相当の切替を行う。
    合成キー入力ではないので Nightcrows のチート対策で弾かれない。
    """
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.1)
    if win32gui.GetForegroundWindow() == hwnd:
        return True
    try:
        ctypes.windll.user32.SwitchToThisWindow(hwnd, True)
    except Exception:
        return False
    time.sleep(0.2)
    return win32gui.GetForegroundWindow() == hwnd

LogFn = Callable[[str], None]

FLOWS_DIR = "flows"
DAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]


# ----------------------------------------------------------------- データモデル
@dataclass
class ScheduleEntry:
    time: str = "00:00"
    target: str = ""
    sequence: list[str] = field(default_factory=list)
    repeat: str = "daily"
    days: list[int] = field(default_factory=list)
    date: str = ""
    enabled: bool = True
    # 続けて実行エントリ。True なら time/repeat/days/date は無視され、
    # schedule リスト上の「直前の非 seq エントリ」が完了した直後に実行される。
    seq: bool = False


@dataclass
class FlowSettings:
    polling_interval_s: float = 1.0


@dataclass
class PcFlow:
    name: str = "untitled"
    version: int = 1
    schedule: list[ScheduleEntry] = field(default_factory=list)
    settings: FlowSettings = field(default_factory=FlowSettings)


# ----------------------------------------------------------------- JSON 入出力
def load_pc_flow(path: str) -> PcFlow:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    sched: list[ScheduleEntry] = []
    for s in data.get("schedule", []) or []:
        sched.append(ScheduleEntry(
            time=s.get("time", "00:00"),
            target=s.get("target", ""),
            sequence=list(s.get("sequence", []) or []),
            repeat=s.get("repeat", "daily"),
            days=list(s.get("days", []) or []),
            date=s.get("date", ""),
            enabled=bool(s.get("enabled", True)),
            seq=bool(s.get("seq", False)),
        ))
    settings_d = data.get("settings", {}) or {}
    return PcFlow(
        name=data.get("name", "untitled"),
        version=int(data.get("version", 1)),
        schedule=sched,
        settings=FlowSettings(
            polling_interval_s=float(settings_d.get("polling_interval_s", 1.0)),
        ),
    )


def save_pc_flow(flow: PcFlow, path: str) -> None:
    rows = []
    for s in flow.schedule:
        row: dict = {"time": s.time, "target": s.target, "repeat": s.repeat}
        if s.sequence:
            row["sequence"] = list(s.sequence)
        if s.repeat == "weekly" and s.days:
            row["days"] = list(s.days)
        if s.repeat == "once" and s.date:
            row["date"] = s.date
        if not s.enabled:
            row["enabled"] = False
        if s.seq:
            row["seq"] = True
        rows.append(row)
    data = {
        "name": flow.name,
        "version": flow.version,
        "schedule": rows,
        "watchers": [],
        "settings": {"polling_interval_s": flow.settings.polling_interval_s},
    }
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ----------------------------------------------------------------- スケジュール
def entry_scenes(entry: ScheduleEntry) -> list[str]:
    """ScheduleEntry の実行シーンリストを返す（target/sequence 混在を吸収）。"""
    if not entry.sequence:
        return [entry.target] if entry.target else []
    if entry.target and entry.target not in entry.sequence:
        return [entry.target] + list(entry.sequence)
    return list(entry.sequence)


def _entry_fingerprint(e: ScheduleEntry) -> tuple:
    """エントリの「実体としての同一性」を表すタプル。

    実行中のフロー差し替え時に「同じエントリ」を見つけて last_fired を継承するために使う。
    enabled は無視（無効化しても「同じエントリ」と見なす）。
    """
    return (
        e.time, e.target, tuple(e.sequence), e.repeat,
        tuple(e.days), e.date, e.seq,
    )


def migrate_last_fired(
    old_flow: PcFlow, new_flow: PcFlow, last_fired: dict[int, date],
) -> dict[int, date]:
    """フロー差し替え時、idx ベースの last_fired を新フローの idx に翻訳する。

    同じ指紋のエントリを探して、既に発火済みなら新 idx へ継承（再発火を防ぐ）。
    削除されたエントリは載らない。新規追加されたエントリは未発火扱い。
    """
    new_lf: dict[int, date] = {}
    new_by_fp: dict[tuple, list[int]] = {}
    for new_idx, ne in enumerate(new_flow.schedule):
        new_by_fp.setdefault(_entry_fingerprint(ne), []).append(new_idx)
    for old_idx, fired_date in last_fired.items():
        if old_idx >= len(old_flow.schedule):
            continue
        fp = _entry_fingerprint(old_flow.schedule[old_idx])
        candidates = new_by_fp.get(fp, [])
        if not candidates:
            continue
        new_lf[candidates.pop(0)] = fired_date   # 多重マッチは消費して二重割当防止
    return new_lf


def check_schedule(
    flow: PcFlow, now: datetime, last_fired: dict[int, date]
) -> tuple[int, ScheduleEntry] | None:
    """今の時刻で発火すべき ScheduleEntry を返す。無ければ None。"""
    today = now.date()
    current_hm = now.strftime("%H:%M")
    today_wd = now.weekday()

    candidates: list[tuple[str, int, ScheduleEntry]] = []
    for idx, entry in enumerate(flow.schedule):
        if not entry.enabled:
            continue
        if entry.seq:
            # 続けて実行エントリは時刻トリガーしない（直前エントリ完了後に連鎖実行）
            continue
        if entry.time > current_hm:
            continue
        if entry.repeat == "once" and entry.date != today.isoformat():
            continue
        if entry.repeat == "weekly" and entry.days and today_wd not in entry.days:
            continue
        if last_fired.get(idx) == today:
            continue
        candidates.append((entry.time, idx, entry))

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    _, idx, entry = candidates[0]
    return idx, entry


def last_due_scenes(
    flow: PcFlow, now: datetime,
) -> tuple[ScheduleEntry, list[str]] | None:
    """現在時刻より前で直近のスケジュールエントリ (entry, シーンリスト) を返す。

    シーン外でウォッチャーが発火し after=restart_scene が要求されたが
    まだ当日シーンが実行されていない（深夜跨ぎ・起動直後など）場合の
    フォールバックに使う。

    曜日フィルタ・enabled / once 日付を考慮し、当日中で最も遅い時刻のエントリを返す。
    seq エントリ単独はトリガー対象外（直前エントリ完了後の連鎖実行用途のため）。
    """
    current_hm = now.strftime("%H:%M")
    today_str = now.date().isoformat()
    today_wd = now.weekday()

    candidates: list[tuple[str, ScheduleEntry, list[str]]] = []
    for idx, entry in enumerate(flow.schedule):
        if not entry.enabled:
            continue
        if entry.seq:
            continue
        if entry.time >= current_hm:
            continue
        if entry.repeat == "once" and entry.date != today_str:
            continue
        if entry.repeat == "weekly" and entry.days and today_wd not in entry.days:
            continue
        scenes = list(entry_scenes(entry))
        # 直後に続く seq エントリも連鎖させる（スケジュール発火時と同じ挙動）
        j = idx + 1
        while j < len(flow.schedule):
            nxt = flow.schedule[j]
            if not nxt.seq:
                break
            if nxt.enabled:
                scenes.extend(entry_scenes(nxt))
            j += 1
        if scenes:
            candidates.append((entry.time, entry, scenes))

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    _, entry, scenes = candidates[0]
    return entry, scenes


def next_schedule_str(flow: PcFlow, now: datetime) -> str:
    """次回発火予定のスケジュール説明文を返す（UI 表示用）。"""
    best_entry: ScheduleEntry | None = None
    best_dt: datetime | None = None

    for entry in flow.schedule:
        if not entry.enabled:
            continue
        if entry.seq:
            # 続けて実行エントリは時刻トリガー対象外
            continue
        try:
            h, m = map(int, entry.time.split(":"))
        except ValueError:
            continue

        if entry.repeat == "daily":
            dt0 = now.replace(hour=h, minute=m, second=0, microsecond=0)
            dt = dt0 if dt0 > now else dt0 + timedelta(days=1)
        elif entry.repeat == "weekly":
            if not entry.days:
                continue
            today_wd = now.weekday()
            dt = None
            for delta in range(7):
                wd = (today_wd + delta) % 7
                if wd not in entry.days:
                    continue
                cdt = datetime.combine(now.date() + timedelta(days=delta),
                                       datetime.min.time()).replace(hour=h, minute=m)
                if cdt > now:
                    dt = cdt
                    break
            if dt is None:
                continue
        elif entry.repeat == "once":
            if not entry.date:
                continue
            try:
                d = datetime.strptime(entry.date, "%Y-%m-%d").date()
            except ValueError:
                continue
            dt = datetime.combine(d, datetime.min.time()).replace(hour=h, minute=m)
            if dt <= now:
                continue
        else:
            continue

        if best_dt is None or dt < best_dt:
            best_dt = dt
            best_entry = entry

    if best_entry is None or best_dt is None:
        return "次回予定: なし"

    scenes = entry_scenes(best_entry)
    name = os.path.splitext(scenes[0])[0] if scenes else best_entry.target
    diff = best_dt - now
    total_min = int(diff.total_seconds() / 60)
    h2, m2 = divmod(total_min, 60)
    remain = f"{h2}h{m2:02d}m" if h2 else f"{m2}分"
    if best_entry.repeat == "weekly" and best_entry.days:
        days_str = "(" + "・".join(DAY_NAMES[d] for d in best_entry.days) + ")"
    elif best_entry.repeat == "daily":
        days_str = "(毎日)"
    elif best_entry.repeat == "once":
        days_str = f"({best_entry.date})"
    else:
        days_str = ""
    return f"次回: {best_entry.time} {name} {days_str}  残り {remain}"


# ----------------------------------------------------------------- フローランナー
class PcFlowRunner(QObject):
    """PC フロースケジューラー（バックグラウンドスレッド）。"""

    log_message           = Signal(str)
    scene_started         = Signal(str, int, int)   # (scene_name, step, total)
    step_updated          = Signal(int, int)         # (current, total)
    state_changed         = Signal(str)              # "idle" | "running"
    next_schedule_changed = Signal(str)
    # (watcher_id, title, today_count, last_fired "HH:MM:SS")
    watcher_fired_visual  = Signal(str, str, int, str)
    # 前面確認ダイアログ要求: (scene_name, done_event)
    # UI 側でダイアログ表示 → set_foreground_choice(choice) → done_event.set() を期待
    foreground_confirm_request = Signal(str, object)

    def __init__(self) -> None:
        super().__init__()
        self._flow: PcFlow | None = None
        self._mouse = None      # PicoMouse | None
        self._window_title = ""
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._current_scene = ""
        self._current_step = 0
        self._total_steps = 0

        # 通知設定（Google Chat Webhook）。空文字なら通知しない。
        self._notify_webhook = ""

        # 単発シーン実行（手動: UI のシーン一覧 / 実行タブの右クリック）
        self._manual_thread: threading.Thread | None = None

        # ウォッチャー関連
        self._watchers: list[PcWatcher] = []
        self._watcher_thread: threading.Thread | None = None
        self._watcher_stop = threading.Event()
        self._watcher_paused = threading.Event()
        self._watcher_pending = threading.Event()
        self._fired_lock = threading.Lock()
        self._fired_queue: list[tuple[PcWatcher, str]] = []   # (watcher, info文字列)
        self._hit_counts: dict[str, int] = {}
        self._last_fired: dict[str, float] = {}

        # 前面確認ダイアログ用（UI スレッドから値をセットしてもらう）
        self._foreground_lock = threading.Lock()
        self._foreground_choice = "run"
        # 直近ダイアログの選択キャッシュ（N 分以内なら再表示せず再利用）
        self._fg_last_choice: str | None = None    # "run" / "skip"（wait はキャッシュ対象外）
        self._fg_last_decision_mono: float = 0.0   # キャッシュ確定時刻 (time.monotonic)
        self._fg_cache_seconds: float = 300.0      # 既定 5 分（UI から変更）

    # ---- 公開 API ----
    @property
    def is_running(self) -> bool:
        """スケジュール実行（メインフロー）が走っているか。"""
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_manual_running(self) -> bool:
        """単発シーン実行（手動）が走っているか。"""
        t = getattr(self, "_manual_thread", None)
        return t is not None and t.is_alive()

    @property
    def is_busy(self) -> bool:
        """スケジュール実行・単発実行のいずれかが進行中か。"""
        return self.is_running or self.is_manual_running

    @property
    def current_scene(self) -> str:
        return self._current_scene

    @property
    def current_step(self) -> tuple[int, int]:
        return self._current_step, self._total_steps

    def set_mouse(self, mouse) -> None:
        self._mouse = mouse

    def set_window_title(self, title: str) -> None:
        self._window_title = title

    def set_notify_webhook(self, url: str) -> None:
        """Google Chat の Webhook URL を設定。空文字で通知無効。"""
        self._notify_webhook = (url or "").strip()

    def load_flow(self, path: str) -> PcFlow:
        flow = load_pc_flow(path)
        self._flow = flow
        self._emit_next_schedule()
        return flow

    def start(self) -> None:
        if self.is_busy:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.state_changed.emit("running")

    def stop(self) -> None:
        """スケジュール / 単発の両方を停止対象とする（_stop_event を共有）。"""
        self._stop_event.set()

    def run_scene_async(self, scene_path: str) -> bool:
        """単一シーンを単発実行する（UI から手動で叩く用）。

        スケジュール実行中・単発実行中は受け付けない。受理時 True。
        scene_path は scenes/ 相対 or 単なる "DQ.json" 等のシーン名。
        """
        if self.is_busy:
            return False
        if not scene_path:
            return False
        self._stop_event.clear()
        self._manual_thread = threading.Thread(
            target=self._run_manual, args=(scene_path,), daemon=True,
        )
        self._manual_thread.start()
        self.state_changed.emit("running")
        return True

    def run_scenes_async(self, scene_paths: list[str]) -> bool:
        """シーン列を順次単発実行する（実行タブ右クリックで親+seq を一括実行する用）。

        スケジュール実行中・単発実行中は受け付けない。受理時 True。
        """
        if self.is_busy:
            return False
        scenes = [s for s in scene_paths if s]
        if not scenes:
            return False
        self._stop_event.clear()
        self._manual_thread = threading.Thread(
            target=self._run_manual_chain, args=(scenes,), daemon=True,
        )
        self._manual_thread.start()
        self.state_changed.emit("running")
        return True

    def _run_manual(self, scene_path: str) -> None:
        self._log(f"単発実行 開始: {scene_path}")
        try:
            ok = self._run_scene(scene_path)
            if self._stop_event.is_set():
                self._log("■ 単発実行 停止 (ユーザー)")
            elif ok:
                self._log("✓ 単発実行 完了")
            else:
                self._log("✗ 単発実行 失敗")
        except Exception as e:
            self._log(f"⚠ 単発実行 例外: {e}")
        finally:
            self._current_scene = ""
            self._current_step = 0
            self._total_steps = 0
            self.state_changed.emit("idle")
            self._emit_next_schedule()

    def _run_manual_chain(self, scenes: list[str]) -> None:
        self._log(f"単発実行 (連鎖 {len(scenes)} 件) 開始")
        try:
            for k, path in enumerate(scenes):
                if self._stop_event.is_set():
                    self._log("■ 単発実行 停止 (ユーザー)")
                    break
                self._log(f"  [{k+1}/{len(scenes)}] {path}")
                ok = self._run_scene(path)
                if not ok and not self._stop_event.is_set():
                    self._log(f"  ✗ {path} 失敗 — 後続スキップ")
                    break
            else:
                self._log("✓ 単発実行 (連鎖) 完了")
        except Exception as e:
            self._log(f"⚠ 単発実行 例外: {e}")
        finally:
            self._current_scene = ""
            self._current_step = 0
            self._total_steps = 0
            self.state_changed.emit("idle")
            self._emit_next_schedule()

    # ---- 内部 ----
    def _should_stop(self) -> bool:
        # ウォッチャー発火もシーン中断トリガになる（後で発火処理）
        return self._stop_event.is_set() or self._watcher_pending.is_set()

    def _log(self, msg: str) -> None:
        write_log(msg)
        self.log_message.emit(msg)

    def _emit_next_schedule(self) -> None:
        if self._flow:
            self.next_schedule_changed.emit(next_schedule_str(self._flow, datetime.now()))
        else:
            self.next_schedule_changed.emit("")

    def _step_cb(self, step: int, total: int) -> None:
        self._current_step = step
        self._total_steps = total
        self.step_updated.emit(step, total)

    # ---- 前面化チェック ----
    def set_foreground_choice(self, choice: str) -> None:
        """UI 側のダイアログから選択結果 ("run" / "wait" / "skip") を受け取る。"""
        with self._foreground_lock:
            self._foreground_choice = choice

    def set_foreground_check_interval_min(self, minutes: float) -> None:
        """前面化確認の再表示間隔（分）。0 でキャッシュ無効。"""
        with self._foreground_lock:
            self._fg_cache_seconds = max(0.0, float(minutes) * 60.0)

    def _request_foreground_confirm(self, scene_name: str) -> str:
        """UI スレッドへダイアログ表示を依頼し、結果を返す。"""
        with self._foreground_lock:
            self._foreground_choice = "run"   # タイムアウト時の既定
        done = threading.Event()
        self.foreground_confirm_request.emit(scene_name, done)
        # UI がダイアログを閉じるまで待機（スレッドはバックグラウンド）
        # stop イベントが先に立てば一旦抜けて skip 扱い
        while not done.is_set():
            if self._stop_event.is_set():
                return "skip"
            if done.wait(timeout=0.5):
                break
        with self._foreground_lock:
            return self._foreground_choice

    def _sleep_with_stop(self, secs: float) -> bool:
        """sleep 中に stop_event が立ったら早期復帰。中断時 False。"""
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return False
            time.sleep(0.5)
        return True

    def _ensure_foreground_or_choose(self, scene_label: str) -> bool:
        """シーン実行前の前面チェック。続行可能なら True、ユーザーがスキップ選択で False。

        前面でなければユーザーへ確認ダイアログ。
        - run: 前面化 → 続行
        - wait: 3 分待機 → 再評価（再帰せずループ）
        - skip: 中止
        - タイムアウト (30 秒): run と同じ

        直近の選択 (run / skip) は `_fg_cache_seconds` の間キャッシュされ、
        その間に再度この関数が呼ばれた場合はダイアログを再表示せず自動適用する。
        ウォッチャーが秒単位で発火する場合に、毎回ダイアログが出ないようにするため。
        """
        if not self._window_title:
            return True
        while True:
            hwnd = find_hwnd_by_title(self._window_title)
            if not hwnd:
                # ウィンドウが見つからない時は前面チェックを諦めて先に進む
                # （run_pc_scene 側で改めてエラー扱いされる）
                return True
            if is_window_foreground(hwnd):
                return True

            # キャッシュ判定: 直近に出した選択が有効期限内なら自動適用
            with self._foreground_lock:
                cache_sec = self._fg_cache_seconds
                last_choice = self._fg_last_choice
                last_mono = self._fg_last_decision_mono
            if (
                cache_sec > 0
                and last_choice in ("run", "skip")
                and (time.monotonic() - last_mono) < cache_sec
            ):
                remain = int(cache_sec - (time.monotonic() - last_mono))
                if last_choice == "run":
                    self._log(
                        f"⚠ 非前面 — キャッシュ「即時実施」を自動適用 "
                        f"(あと {remain}s で再確認)"
                    )
                    if bring_window_to_foreground(hwnd):
                        self._log("✓ 対象ウィンドウを前面化しました")
                    return True
                else:  # skip
                    self._log(
                        f"⊘ 非前面 — キャッシュ「スキップ」を自動適用 "
                        f"(あと {remain}s で再確認)"
                    )
                    return False

            self._log("⚠ 対象ウィンドウが前面にありません — ユーザー確認待ち")
            choice = self._request_foreground_confirm(scene_label)
            if self._stop_event.is_set():
                return False
            if choice == "run":
                with self._foreground_lock:
                    self._fg_last_choice = "run"
                    self._fg_last_decision_mono = time.monotonic()
                if bring_window_to_foreground(hwnd):
                    self._log("✓ 対象ウィンドウを前面化しました")
                else:
                    self._log("⚠ 前面化に失敗 — そのまま続行します")
                return True
            if choice == "skip":
                with self._foreground_lock:
                    self._fg_last_choice = "skip"
                    self._fg_last_decision_mono = time.monotonic()
                self._log("⊘ ユーザー選択によりシーン実行をスキップ")
                return False
            if choice == "wait":
                # wait はキャッシュ対象外（毎回再評価）
                self._log("⏸ 3 分待機して再評価します")
                from .foreground_dialog import WAIT_SLEEP_SECONDS
                if not self._sleep_with_stop(WAIT_SLEEP_SECONDS):
                    return False
                # 待機後、while 先頭に戻って再評価
                continue
            # 想定外の値は run 扱い
            return True

    def _run_scene(self, path: str) -> bool:
        # scenes/ プレフィックスが二重にならないよう正規化
        norm = path
        for sep in ("/", "\\"):
            if norm.startswith(SCENES_DIR + sep):
                norm = norm[len(SCENES_DIR) + 1:]
                break
        full = os.path.join(SCENES_DIR, norm)

        try:
            scene = load_pc_scene(full)
        except Exception as e:
            self._log(f"シーン読込失敗: {path}: {e}")
            return False

        self._current_scene = scene.name
        self._current_step = 0
        self._total_steps = len(scene.steps)
        self.scene_started.emit(scene.name, 0, len(scene.steps))

        hwnd = find_hwnd_by_title(self._window_title) if self._window_title else None
        return run_pc_scene(
            scene,
            mouse=self._mouse,
            hwnd=hwnd,
            log=self._log,
            should_stop=self._should_stop,
            step_callback=self._step_cb,
        )

    # ---------------------------------------------- ウォッチャーポーリング
    def _start_watcher_thread(self) -> None:
        # watchers/ から有効なものだけ読み込む
        loaded = [w for _, w in list_pc_watchers() if w.enabled]
        if not loaded:
            self._log("ウォッチャー: 0 件（バックグラウンド監視は無効）")
            return
        self._watchers = loaded
        self._log(f"ウォッチャー: {len(loaded)} 件を監視開始")
        self._watcher_stop.clear()
        self._watcher_paused.clear()
        self._hit_counts.clear()
        self._last_fired.clear()
        with self._fired_lock:
            self._fired_queue.clear()
        self._watcher_pending.clear()
        self._watcher_thread = threading.Thread(
            target=self._watcher_loop, daemon=True,
        )
        self._watcher_thread.start()

    def _stop_watcher_thread(self) -> None:
        self._watcher_stop.set()
        t = self._watcher_thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self._watcher_thread = None
        self._watchers = []

    def _watcher_loop(self) -> None:
        while not self._watcher_stop.is_set():
            if self._watcher_paused.is_set():
                time.sleep(0.2)
                continue
            hwnd = find_hwnd_by_title(self._window_title) if self._window_title else None
            if not hwnd:
                time.sleep(1.0)
                continue
            img = capture_window(hwnd)
            if img is None:
                time.sleep(0.5)
                continue

            now = time.monotonic()
            min_interval = 60.0
            for w in self._watchers:
                if self._watcher_stop.is_set():
                    break
                # 冷却中はスキップ
                if (self._last_fired.get(w.id, 0.0) + w.cooldown_s) > now:
                    continue
                try:
                    r = evaluate_watcher(img, w)
                except Exception as e:
                    self._log(f"ウォッチャー評価例外 [{w.title}]: {e}")
                    continue
                need = max(1, w.condition.consecutive)
                if r.fired:
                    self._hit_counts[w.id] = self._hit_counts.get(w.id, 0) + 1
                    if self._hit_counts[w.id] >= need:
                        self._hit_counts[w.id] = 0
                        self._last_fired[w.id] = now
                        info = self._fmt_eval(w, r)
                        with self._fired_lock:
                            self._fired_queue.append((w, info))
                        self._watcher_pending.set()
                        try:
                            today_count, fired_at = record_watcher_fire(w.id)
                        except Exception:
                            today_count, fired_at = 0, ""
                        self._log(
                            f"🔥 ウォッチャー発火: [{w.title}] {info} "
                            f"(本日 {today_count} 回目)"
                        )
                        self.watcher_fired_visual.emit(
                            w.id, w.title, today_count, fired_at,
                        )
                        self._notify_watcher_fired(w, info)
                else:
                    self._hit_counts[w.id] = 0
                # 個別ウォッチャーの最短 poll を集約してスリープ時間に使う
                wp = max(0.2, min(w.poll_min_s, w.poll_max_s) or 1.0)
                if wp < min_interval:
                    min_interval = wp

            # 全ウォッチャーの最短間隔（min〜max からランダム）でスリープ
            if self._watchers:
                pmin = min(max(0.2, w.poll_min_s) for w in self._watchers)
                pmax = max(pmin, max(w.poll_max_s for w in self._watchers))
                interval = random.uniform(pmin, pmax)
            else:
                interval = 1.0
            waited = 0.0
            while waited < interval and not self._watcher_stop.is_set():
                time.sleep(min(0.1, interval - waited))
                waited += 0.1

    def _notify_watcher_fired(self, w: PcWatcher, info: str) -> None:
        """Google Chat へウォッチャー発火を非同期通知する（設定があれば）。"""
        url = self._notify_webhook
        if not url:
            return
        # フロー名・対象ウィンドウを補助情報として添える
        flow_name = self._flow.name if self._flow else "(不明)"
        cond = w.condition.type if w.condition else "?"
        body = (
            f"条件: {cond}\n"
            f"判定: {info}\n"
            f"フロー: {flow_name}\n"
            f"対象: {self._window_title or '(未設定)'}"
        )

        def _worker() -> None:
            ok, msg = send_google_chat(url, f"ウォッチャー発火: {w.title}", body)
            if not ok:
                self.log_message.emit(f"⚠ Google Chat 通知失敗: {msg}")

        threading.Thread(target=_worker, daemon=True).start()

    @staticmethod
    def _fmt_eval(w: PcWatcher, r) -> str:
        c = w.condition
        if c.type in ("image_appear", "image_gone"):
            score = f"{r.score:.3f}" if r.score is not None else "—"
            return f"score={score} threshold={c.threshold:.2f}"
        if c.type == "ocr_number":
            val = f"{r.value:.0f}" if r.value is not None else "—"
            return f"値={val} {c.op} {c.value:.0f}"
        return ""

    def _pop_fired(self) -> tuple[PcWatcher, str] | None:
        """発火キューから優先度順で 1 件取り出す。"""
        with self._fired_lock:
            if not self._fired_queue:
                self._watcher_pending.clear()
                return None
            self._fired_queue.sort(key=lambda t: -t[0].priority)
            item = self._fired_queue.pop(0)
            if not self._fired_queue:
                self._watcher_pending.clear()
            return item

    def _handle_fired(self, w: PcWatcher, info: str) -> str:
        """発火を1件処理して、after アクション文字列を返す。"""
        if w.handler:
            handler_label = os.path.splitext(os.path.basename(w.handler))[0]
            # ハンドラー実行直前にも前面化チェック（ユーザー操作中の暴発を防ぐ）
            if not self._ensure_foreground_or_choose(handler_label):
                self._log("⊘ ハンドラー実行をスキップ（前面化キャンセル）")
                return "noop"
            self._watcher_paused.set()
            try:
                self._log(f"ハンドラー実行: {w.handler}")
                self._run_scene(w.handler)
            finally:
                self._watcher_paused.clear()
        return w.after or "noop"

    # ---------------------------------------------- メインループ
    def _run(self) -> None:
        flow = self._flow
        if flow is None:
            self._log("フロー未読み込み")
            self.state_changed.emit("idle")
            return

        self._log(f"フロー開始: {flow.name}")
        last_fired: dict[int, date] = {}

        # 起動時刻より前のエントリをスキップ（再起動直後の重複実行を防ぐ）
        now_start = datetime.now()
        hm_start = now_start.strftime("%H:%M")
        wd_start = now_start.weekday()
        today_start = now_start.date()
        for idx, entry in enumerate(flow.schedule):
            if entry.time >= hm_start:
                continue
            if entry.repeat == "daily":
                last_fired[idx] = today_start
            elif entry.repeat == "weekly":
                if not entry.days or wd_start in entry.days:
                    last_fired[idx] = today_start
            elif entry.repeat == "once":
                if entry.date == today_start.isoformat():
                    last_fired[idx] = today_start
        if last_fired:
            self._log(f"起動時刻 {hm_start} より前のスケジュール {len(last_fired)} 件をスキップ")

        poll = max(0.5, flow.settings.polling_interval_s)

        # ウォッチャー監視を開始
        self._start_watcher_thread()

        try:
            while not self._stop_event.is_set():
                # フローが「保存して反映」等で差し替えられたか検出
                if self._flow is not None and self._flow is not flow:
                    new_flow = self._flow
                    last_fired = migrate_last_fired(flow, new_flow, last_fired)
                    poll = max(0.5, new_flow.settings.polling_interval_s)
                    self._log(
                        f"フロー再ロード反映: {new_flow.name} "
                        f"({len(new_flow.schedule)} エントリ, 発火継承 {len(last_fired)} 件)"
                    )
                    flow = new_flow
                    self._emit_next_schedule()

                # 発火が溜まっていれば最初に処理（シーン外でも反応）
                fired = self._pop_fired()
                if fired:
                    w, info = fired
                    action = self._handle_fired(w, info)
                    if action == "stop":
                        self._log("after=stop によりフロー停止")
                        break
                    if action == "restart_scene":
                        # シーン外で restart_scene が要求された場合のフォールバック:
                        # 当日（深夜跨ぎ含む）の直近スケジュールエントリへ戻る
                        fb = last_due_scenes(flow, datetime.now())
                        if fb is not None:
                            fb_entry, fb_scenes = fb
                            self._log(
                                f"after=restart_scene: 未実行のため直近スケジュール "
                                f"{fb_entry.time} → {fb_scenes} を実行"
                            )
                            if not self._run_scenes_with_watcher(fb_scenes):
                                break
                        else:
                            self._log(
                                "after=restart_scene: 直近スケジュールが見つからずスキップ"
                            )
                    # next_scene はシーン外では noop と同じ扱い
                    continue

                result = check_schedule(flow, datetime.now(), last_fired)
                if result is not None:
                    idx, entry = result
                    today = datetime.now().date()
                    last_fired[idx] = today
                    scenes = list(entry_scenes(entry))
                    # 直後に続く seq エントリのシーンを連結（disabled はスキップ）
                    chained: list[str] = []
                    j = idx + 1
                    while j < len(flow.schedule):
                        nxt = flow.schedule[j]
                        if not nxt.seq:
                            break
                        last_fired[j] = today
                        if nxt.enabled:
                            ns = entry_scenes(nxt)
                            scenes.extend(ns)
                            chained.extend(ns)
                        j += 1
                    if chained:
                        self._log(
                            f"スケジュール発火: {entry.time} → "
                            f"{entry_scenes(entry)} + 続けて {chained}"
                        )
                    else:
                        self._log(f"スケジュール発火: {entry.time} → {scenes}")
                    if not self._run_scenes_with_watcher(scenes):
                        # stop アクションが発火
                        break
                    self._emit_next_schedule()
                    continue

                # ポーリング待機（0.1 秒刻みで停止 / 発火を確認）
                for _ in range(max(1, int(poll * 10))):
                    if self._stop_event.is_set() or self._watcher_pending.is_set():
                        break
                    time.sleep(0.1)
                self._emit_next_schedule()
        finally:
            self._stop_watcher_thread()
            self._current_scene = ""
            self._current_step = 0
            self._total_steps = 0
            self.state_changed.emit("idle")
            self._log("フロー停止")

    def _run_scenes_with_watcher(self, scenes: list[str]) -> bool:
        """シーン列を順次実行し、途中の発火を処理する。

        戻り値 True=継続、False=フロー停止要求 (after=stop)
        """
        k = 0
        while k < len(scenes):
            if self._stop_event.is_set():
                return True
            path = scenes[k]
            scene_label = os.path.splitext(os.path.basename(path))[0]
            # シーン実行直前に対象ウィンドウの前面化チェック（ユーザー確認あり）
            if not self._ensure_foreground_or_choose(scene_label):
                k += 1
                continue
            self._log(f"シーン実行 [{k+1}/{len(scenes)}]: {path}")
            self._run_scene(path)
            # シーン終了後（または発火による中断後）、発火キューを処理
            consumed_any = False
            while True:
                fired = self._pop_fired()
                if fired is None:
                    break
                consumed_any = True
                w, info = fired
                action = self._handle_fired(w, info)
                if action == "stop":
                    return False
                if action == "restart_scene":
                    self._log(f"after=restart_scene: シーン {path} を再実行")
                    k -= 1  # 後の k += 1 で同じ index に戻る
                    break
                if action == "next_scene":
                    self._log("after=next_scene: 次シーンへ")
                    break
                # noop: 中断した場合は本来の sequence を続けるためそのまま
            k += 1
        return True
