"""PC フロースケジューラー。

mobile/gui/flow.py と同じ JSON フォーマット（schedule・settings）を扱う。
mobile/ への依存を持たず PC 単体で動作するよう、必要な部分を再実装している。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

from PySide6.QtCore import QObject, Signal

from .pc_scene import SCENES_DIR, load_pc_scene, run_pc_scene
from .window_picker import find_hwnd_by_title

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


def next_schedule_str(flow: PcFlow, now: datetime) -> str:
    """次回発火予定のスケジュール説明文を返す（UI 表示用）。"""
    best_entry: ScheduleEntry | None = None
    best_dt: datetime | None = None

    for entry in flow.schedule:
        if not entry.enabled:
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

    # ---- 公開 API ----
    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

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

    def load_flow(self, path: str) -> PcFlow:
        flow = load_pc_flow(path)
        self._flow = flow
        self._emit_next_schedule()
        return flow

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.state_changed.emit("running")

    def stop(self) -> None:
        self._stop_event.set()

    # ---- 内部 ----
    def _should_stop(self) -> bool:
        return self._stop_event.is_set()

    def _log(self, msg: str) -> None:
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

        try:
            while not self._should_stop():
                result = check_schedule(flow, datetime.now(), last_fired)
                if result is not None:
                    idx, entry = result
                    last_fired[idx] = datetime.now().date()
                    scenes = entry_scenes(entry)
                    self._log(f"スケジュール発火: {entry.time} → {scenes}")
                    for k, path in enumerate(scenes):
                        if self._should_stop():
                            break
                        self._log(f"シーン実行 [{k+1}/{len(scenes)}]: {path}")
                        self._run_scene(path)
                    self._emit_next_schedule()
                    continue

                # ポーリング待機（0.1 秒刻みで停止を確認）
                for _ in range(max(1, int(poll * 10))):
                    if self._should_stop():
                        break
                    time.sleep(0.1)
                self._emit_next_schedule()
        finally:
            self._current_scene = ""
            self._current_step = 0
            self._total_steps = 0
            self.state_changed.emit("idle")
            self._log("フロー停止")
