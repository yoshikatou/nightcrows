"""フロー再生エンジン。

Scene を順次実行する `replay_scene` の上に、以下を積む：
- main_sequence 順次再生
- schedule（時刻）割り込み — ステップ境界で評価し、発火したらシーンを中断して target にジャンプ
- watchers（常時監視）— 別スレッドで定期 screencap → テンプレマッチ、
  発火したらシーンを中断してハンドラ実行。優先度/クールダウン/pause サポート
- after_main: "stay" は最後のシーンを繰り返す / "stop" は終了

実装済み condition タイプ:
- image_appear
- image_gone （N回連続で不検出なら発火）
- digit_threshold（0.png〜9.png でテンプレマッチして数値比較）
- ocr_number（Tesseract OCR で数値を読んで閾値比較）

未実装: interrupt="immediate"
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

import cv2
import numpy as np

from .adb import screencap
from .flow import Condition, Flow, ScheduleEntry, Watcher
from .maintenance import MaintenanceEntry, is_in_maintenance
from .replay import replay_scene
from .scene import load_scene

SCENES_DIR = "scenes"

LogFn = Callable[[str], None]
StopFn = Callable[[], bool]


def _scene_path(rel: str) -> str:
    """scenes/ からの相対パスを実際のパスに。
    handler に "scenes/" プレフィックスが含まれていても二重にならないよう吸収する。
    """
    for sep in ("/", "\\"):
        prefix = SCENES_DIR + sep
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
            break
    return os.path.join(SCENES_DIR, rel)


def _entry_scenes(entry: "ScheduleEntry") -> list[str]:
    """ScheduleEntry の実行シーンリストを返す。

    target と sequence が混在するケース（旧形式データ）を吸収する:
    - sequence が空 → [target]
    - sequence が非空で target が含まれていない → [target] + sequence
    - sequence が非空で target が含まれている   → sequence そのまま
    """
    if not entry.sequence:
        return [entry.target] if entry.target else []
    if entry.target and entry.target not in entry.sequence:
        return [entry.target] + list(entry.sequence)
    return list(entry.sequence)


# ============================================================ schedule
def _check_schedule(
    flow: Flow, now: datetime, last_fired: dict[int, date]
) -> tuple[int, ScheduleEntry] | None:
    """今の時刻で発火すべき schedule を返す。無ければ None。"""
    today = now.date()
    today_str = today.isoformat()
    current_hm = now.strftime("%H:%M")

    today_weekday = now.weekday()  # 0=月〜6=日

    candidates: list[tuple[str, int, ScheduleEntry]] = []
    for idx, entry in enumerate(flow.schedule):
        if not entry.enabled:
            continue
        if entry.time > current_hm:
            continue
        if entry.repeat == "once":
            if entry.date != today_str:
                continue
        elif entry.repeat == "weekly":
            if entry.days and today_weekday not in entry.days:
                continue
        if last_fired.get(idx) == today:
            continue
        candidates.append((entry.time, idx, entry))

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    _, idx, entry = candidates[0]
    return idx, entry


# ============================================================ watcher
def _evaluate_condition(condition: Condition, img: np.ndarray) -> bool:
    """今の画面（img）に対して condition の単発判定を返す。

    image_gone の「N回連続」集計は呼び出し側が行う（WatcherState._run）。
    """
    if condition.type == "image_appear":
        return _image_appear(condition, img)
    if condition.type == "image_gone":
        # この瞬間「見えていない」なら True（発火予備状態）。
        return not _image_appear(condition, img)
    if condition.type == "digit_threshold":
        return _digit_threshold(condition, img)
    if condition.type == "ocr_number":
        return _ocr_number(condition, img)
    return False


def _image_appear(c: Condition, img: np.ndarray) -> bool:
    tmpl = cv2.imread(c.template, cv2.IMREAD_COLOR)
    if tmpl is None:
        return False
    target = img
    if c.region and len(c.region) == 4:
        x, y, w, h = c.region
        h_img, w_img = img.shape[:2]
        x2 = min(x + w, w_img)
        y2 = min(y + h, h_img)
        x = max(0, x)
        y = max(0, y)
        target = img[y:y2, x:x2]
    if target.shape[0] < tmpl.shape[0] or target.shape[1] < tmpl.shape[1]:
        return False
    res = cv2.matchTemplate(target, tmpl, cv2.TM_CCOEFF_NORMED)
    _, maxv, _, _ = cv2.minMaxLoc(res)
    return maxv >= c.threshold


def _digit_threshold(c: Condition, img: np.ndarray) -> bool:
    """region 内の数字を OCR（テンプレマッチ）で読み、op/value と比較。"""
    num = _read_digits(c, img)
    if num is None:
        return False
    return _compare(num, c.op, c.value)


def _read_digits(c: Condition, img: np.ndarray,
                 match_threshold: float = 0.8) -> int | None:
    """region を切り出し、0.png〜9.png でテンプレマッチして整数を返す。

    見つからない / 読めないときは None。
    """
    if not c.digits_dir:
        return None
    if not c.region or len(c.region) != 4:
        return None
    x, y, w, h = c.region
    h_img, w_img = img.shape[:2]
    x2 = min(x + w, w_img)
    y2 = min(y + h, h_img)
    x = max(0, x)
    y = max(0, y)
    target = img[y:y2, x:x2]
    if target.size == 0:
        return None

    matches: list[tuple[int, int, float]] = []  # (x, digit, score)
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
        ys, xs = np.where(res >= match_threshold)
        for yi, xi in zip(ys, xs):
            matches.append((int(xi), d, float(res[yi, xi])))

    if not matches:
        return None

    # x 順でソートして、同じ位置（tmpl_w/2 以内）の重複は高スコアのみ残す
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
        return None
    digits_str = "".join(str(m[1]) for m in filtered)
    try:
        return int(digits_str)
    except ValueError:
        return None


def _preprocess_for_ocr(crop: np.ndarray) -> list[np.ndarray]:
    """OCR用前処理バリアントを返す（テストダイアログからも使用）。

    バリアント順:
      0: Otsu 二値化（既存）
      1: Otsu 反転 — Otsu が明暗を誤判定したときの救済
      2: ぼかし後 Otsu — ノイズ・アンチエイリアスを平滑化
      3: 適応的二値化 — グラデーション背景に強い
    """
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, v0 = cv2.threshold(gray,    0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, v2 = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    v3 = cv2.adaptiveThreshold(
        blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )
    return [v0, cv2.bitwise_not(v0), v2, v3]


_OCR_VARIANT_NAMES = ["Otsu", "Otsu反転", "Otsu+ぼかし", "適応的"]


def _ocr_digits_best(crop: np.ndarray, config: str) -> tuple[str | None, int]:
    """複数の前処理バリアントで OCR を試み、最も長い数字列とバリアント番号を返す。

    Returns:
        (digits_str, variant_index) — 読み取れなければ (None, -1)
    """
    try:
        import pytesseract
    except ImportError:
        return None, -1
    best_digits: str | None = None
    best_idx = -1
    for i, v in enumerate(_preprocess_for_ocr(crop)):
        try:
            text = pytesseract.image_to_string(v, config=config).strip()
            digits = "".join(ch for ch in text if ch.isdigit())
            if digits and (best_digits is None or len(digits) > len(best_digits)):
                best_digits = digits
                best_idx = i
        except Exception:
            continue
    return best_digits, best_idx


def _ocr_number(c: Condition, img: np.ndarray) -> bool:
    """Tesseract OCR で region 内の数値を読み、op/value と比較。"""
    if not c.region or len(c.region) != 4:
        return False
    x, y, w, h = c.region
    h_img, w_img = img.shape[:2]
    crop = img[max(0, y):min(y + h, h_img), max(0, x):min(x + w, w_img)]
    if crop.size == 0:
        return False
    config = "--psm 7 --oem 3"
    wl = (c.ocr_whitelist or "").strip()
    if wl:
        config += f" -c tessedit_char_whitelist={wl}"
    digits, _ = _ocr_digits_best(crop, config)
    if not digits:
        return False
    return _compare(int(digits), c.op, c.value)


def _read_ocr_value(c: Condition, img: np.ndarray) -> int | None:
    """ocr_number / digit_threshold の読み取り値だけを返す（比較なし）。ログ用。"""
    if c.type == "digit_threshold":
        return _read_digits(c, img)
    if c.type == "ocr_number":
        if not c.region or len(c.region) != 4:
            return None
        x, y, w, h = c.region
        h_img, w_img = img.shape[:2]
        crop = img[max(0, y):min(y + h, h_img), max(0, x):min(x + w, w_img)]
        if crop.size == 0:
            return None
        config = "--psm 7 --oem 3"
        wl = (c.ocr_whitelist or "").strip()
        if wl:
            config += f" -c tessedit_char_whitelist={wl}"
        digits, _ = _ocr_digits_best(crop, config)
        return int(digits) if digits else None
    return None


def _compare(a: int, op: str, b: int) -> bool:
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "==":
        return a == b
    return False


class WatcherState:
    """ウォッチャーの状態管理 + バックグラウンドポーリングスレッド。"""

    def __init__(self, flow: Flow, serial: str, log: LogFn) -> None:
        self._flow = flow
        self._serial = serial
        self._log = log
        self._enabled: list[Watcher] = [w for w in flow.watchers if w.enabled]
        self._last_fired_mono: dict[str, float] = {}  # watcher.id -> time
        self._miss_count: dict[str, int] = {}         # image_gone の連続外れ回数
        self._hit_count:  dict[str, int] = {}         # ocr_number / digit_threshold の連続ヒット回数
        self._fired_queue: deque[Watcher] = deque()
        self._paused = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_s = max(0.1, float(flow.settings.polling_interval_s or 1.0))

    def start(self) -> None:
        if not self._enabled:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def pop_fired(self) -> Watcher | None:
        try:
            return self._fired_queue.popleft()
        except IndexError:
            return None

    def drain(self) -> None:
        self._fired_queue.clear()

    def mark_fired(self, watcher_id: str) -> None:
        """ハンドラ終了時に呼ぶ。cooldown の起点とカウンタをリセット。"""
        self._last_fired_mono[watcher_id] = time.monotonic()
        self._miss_count[watcher_id] = 0
        self._hit_count[watcher_id]  = 0

    # ------------------------------------------------------------------ impl
    def _next_interval(self, w: "Watcher") -> float:
        """ウォッチャーの次回ポーリング間隔を返す。個別設定がなければ全体設定を使う。"""
        lo = w.poll_min_s if w.poll_min_s > 0 else self._poll_s
        hi = w.poll_max_s if w.poll_max_s > w.poll_min_s else lo
        if hi <= lo:
            return lo
        import random as _r
        return _r.uniform(lo, hi)

    def _run(self) -> None:
        # ウォッチャーごとの次回評価時刻（未設定=即時評価）
        next_eval: dict[str, float] = {}

        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(0.1)
                continue

            now = time.monotonic()
            due = [w for w in self._enabled if now >= next_eval.get(w.id, 0.0)]

            if not due:
                # 次に評価が必要なウォッチャーまで待つ（最大 0.1 秒）
                if self._enabled:
                    nxt = min(next_eval.get(w.id, now) for w in self._enabled)
                    sleep_s = max(0.0, nxt - now)
                else:
                    sleep_s = self._poll_s
                time.sleep(min(sleep_s, 0.1))
                continue

            try:
                png = screencap(self._serial)
                arr = np.frombuffer(png, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            except Exception as e:
                self._log(f"  watcher screencap エラー: {e}")
                snap = time.monotonic()
                for w in due:
                    next_eval[w.id] = snap + self._next_interval(w)
                continue
            if img is None:
                snap = time.monotonic()
                for w in due:
                    next_eval[w.id] = snap + self._next_interval(w)
                continue
            # 部分破損PNG（デコードできるが画面サイズ未満）は捨てる
            h_got, w_got = img.shape[:2]
            if h_got < 200 or w_got < 200:
                self._log(f"  watcher スクショ異常サイズ {w_got}x{h_got} — スキップ")
                snap = time.monotonic()
                for w in due:
                    next_eval[w.id] = snap + self._next_interval(w)
                continue

            snap = time.monotonic()
            fires: list[Watcher] = []
            for w in due:
                # 次回評価時刻を即座に設定（評価時間を除いた間隔）
                next_eval[w.id] = snap + self._next_interval(w)

                if self._paused.is_set():
                    break
                last = self._last_fired_mono.get(w.id, 0.0)
                if last + w.cooldown_s > snap:
                    continue
                if any(q.id == w.id for q in self._fired_queue):
                    continue
                wname = w.title or w.id
                try:
                    single = _evaluate_condition(w.condition, img)
                except Exception as e:
                    self._log(f"  watcher [{wname}] 評価エラー: {e}")
                    continue

                if w.condition.type == "image_gone":
                    required = max(1, int(w.condition.consecutive or 1))
                    if single:
                        self._miss_count[w.id] = self._miss_count.get(w.id, 0) + 1
                        if self._miss_count[w.id] >= required:
                            fires.append(w)
                    else:
                        self._miss_count[w.id] = 0
                elif w.condition.type in ("ocr_number", "digit_threshold"):
                    required = max(1, int(w.condition.consecutive or 1))
                    val = _read_ocr_value(w.condition, img)
                    val_str = f" 読取値={val}" if val is not None else ""
                    cond_str = f" ({w.condition.op}{w.condition.value})"
                    if single:
                        self._hit_count[w.id] = self._hit_count.get(w.id, 0) + 1
                        cnt = self._hit_count[w.id]
                        self._log(f"  👁 [{wname}] 連続ヒット {cnt}/{required}{val_str}{cond_str}")
                        if cnt >= required:
                            fires.append(w)
                    else:
                        if self._hit_count.get(w.id, 0) > 0:
                            self._log(f"  👁 [{wname}] 条件外れ — カウンタリセット{val_str}{cond_str}")
                        self._hit_count[w.id] = 0
                else:
                    if single:
                        fires.append(w)

            if fires:
                fires.sort(key=lambda w: (-w.priority, self._enabled.index(w)))
                winner = fires[0]
                self._last_fired_mono[winner.id] = time.monotonic()
                self._fired_queue.append(winner)
                wname = winner.title or winner.id
                self._log(f"  👁 watcher 発火検知: [{wname}] "
                          f"(priority={winner.priority})")


# ============================================================ ヘルパー
def _last_due_scenes(flow: Flow, now: datetime) -> tuple[ScheduleEntry, list[str]] | None:
    """現在時刻より前で直近のスケジュールエントリ (entry, シーンリスト) を返す。

    フロー開始時にスケジュールを全スキップした場合など、まだシーンが実行されて
    いないときの restart_scene フォールバックに使う。
    """
    today_str = now.date().isoformat()
    current_hm = now.strftime("%H:%M")
    today_wd = now.weekday()

    candidates: list[tuple[str, ScheduleEntry, list[str]]] = []
    for entry in flow.schedule:
        if not entry.enabled:
            continue
        if entry.time >= current_hm:
            continue
        if entry.repeat == "once" and entry.date != today_str:
            continue
        if entry.repeat == "weekly" and entry.days and today_wd not in entry.days:
            continue
        scenes = _entry_scenes(entry)
        if scenes:
            candidates.append((entry.time, entry, list(scenes)))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, entry, scenes = candidates[0]
    return entry, scenes


def _entry_started_at(entry: ScheduleEntry, now: datetime) -> datetime:
    """エントリの「起動基準時刻」 = 今日の entry.time。"""
    try:
        h, m = map(int, entry.time.split(":"))
    except ValueError:
        return now
    return datetime.combine(now.date(), datetime.min.time()).replace(hour=h, minute=m)


# ============================================================ events
@dataclass
class _ScheduleEvent:
    scenes: list[str]                       # 実行するシーンの順序リスト
    retry_policy: str = "always"            # 親エントリの retry_policy
    retry_window_min: int = 0               # 親エントリの retry_window_min
    started_at: datetime | None = None      # 起動基準時刻（window 判定用）


@dataclass
class _WatcherEvent:
    watcher: Watcher


_PendingEvent = "_ScheduleEvent | _WatcherEvent"


# ============================================================ main
def _wait_maintenance(
    entry: MaintenanceEntry, log: LogFn, should_stop: StopFn
) -> None:
    """メンテ終了時刻まで 30 秒ごとに待機する。"""
    log(f"🔧 メンテナンス中: {entry.label}  {entry.start} 〜 {entry.end}")
    while not should_stop():
        now = datetime.now()
        if is_in_maintenance([entry], now) is None:
            log("🔧 メンテナンス終了 — 再開します")
            return
        remaining = datetime.strptime(entry.end, "%Y-%m-%d %H:%M") - now
        mins = int(remaining.total_seconds() // 60)
        log(f"🔧 メンテ待機中 … 残り約 {mins} 分")
        for _ in range(30):
            if should_stop():
                return
            time.sleep(1)


def replay_flow(
    flow: Flow, serial: str,
    log: LogFn = print,
    should_stop: StopFn = lambda: False,
    maintenance: list[MaintenanceEntry] | None = None,
    notify_fn: "Callable[[str, str], None] | None" = None,
) -> None:
    """フローを再生する。"""
    schedule_only = not flow.main_sequence
    if schedule_only:
        log("main_sequence なし — スケジュール＋ウォッチャー監視モードで起動")

    last_fired_schedule: dict[int, date] = {}
    pending: list[object] = []

    # 起動時点で時刻が過ぎているエントリは本日分を発火済みとしてスキップ
    _now_start = datetime.now()
    _today_start = _now_start.date()
    _hm_start = _now_start.strftime("%H:%M")
    _wd_start = _now_start.weekday()
    for _idx, _entry in enumerate(flow.schedule):
        if _entry.time >= _hm_start:
            continue
        if _entry.repeat == "daily":
            last_fired_schedule[_idx] = _today_start
        elif _entry.repeat == "weekly":
            if not _entry.days or _wd_start in _entry.days:
                last_fired_schedule[_idx] = _today_start
        elif _entry.repeat == "once":
            if _entry.date == _today_start.isoformat():
                last_fired_schedule[_idx] = _today_start
    skipped = sum(1 for v in last_fired_schedule.values() if v == _today_start)
    if skipped:
        log(f"⏭ 起動時刻 {_hm_start} より前のスケジュール {skipped} 件をスキップ")

    watcher_state = WatcherState(flow, serial, log)
    watcher_state.start()

    def scene_interrupt() -> bool:
        """replay_scene に渡すストップ判定。schedule と watcher を評価。"""
        if should_stop():
            return True
        if maintenance and is_in_maintenance(maintenance, datetime.now()):
            return True
        fired = _check_schedule(flow, datetime.now(), last_fired_schedule)
        if fired is not None:
            idx, entry = fired
            last_fired_schedule[idx] = datetime.now().date()
            scenes = _entry_scenes(entry)
            pending.append(_ScheduleEvent(
                scenes=scenes,
                retry_policy=entry.retry_policy,
                retry_window_min=entry.retry_window_min,
                started_at=_entry_started_at(entry, datetime.now()),
            ))
            log(f"📅 スケジュール発火 (step_end 割り込み): "
                f"{entry.time} → {scenes}")
            return True
        w = watcher_state.pop_fired()
        if w is not None:
            pending.append(_WatcherEvent(watcher=w))
            log(f"👁 watcher 発火 (step_end 割り込み): [{w.title or w.id}] → {w.handler}")
            if w.alert_desktop and notify_fn:
                notify_fn(f"ウォッチャー発火: {w.title}", w.handler or "")
            return True
        return False

    # pick_scene の順番モード用カウンタ（フロー実行中は持続）
    seq_state: dict[str, int] = {}

    def run_scene(path: str, label: str) -> None:
        try:
            scene = load_scene(_scene_path(path))
        except Exception as e:
            log(f"▶ {label}: {path}")
            log(f"  シーン読込失敗: {path}: {e}")
            return
        log(f"▶ {label}: {scene.name}  ({path})")
        replay_scene(scene, serial, log=log, should_stop=scene_interrupt,
                     _seq_state=seq_state)

    current_idx = 0
    main_seq = flow.main_sequence
    last_running_scene: str | None = None   # restart_scene 用：直前に実行したシーンパス
    # シーン path → 親エントリ由来の retry policy 情報
    scene_policies: dict[str, dict] = {}

    try:
        while not should_stop():
            # 0. メンテナンスチェック
            if maintenance:
                m = is_in_maintenance(maintenance, datetime.now())
                if m is not None:
                    _wait_maintenance(m, log, should_stop)
                    continue

            # 1. pending イベント処理
            if pending:
                event = pending.pop(0)
                if isinstance(event, _ScheduleEvent):
                    # 親エントリの policy をシーケンス全シーンに伝搬
                    started = event.started_at or datetime.now()
                    for path in event.scenes:
                        scene_policies[path] = {
                            "policy": event.retry_policy,
                            "window_min": event.retry_window_min,
                            "started_at": started,
                        }
                    for i, path in enumerate(event.scenes):
                        if should_stop():
                            break
                        last_running_scene = path
                        run_scene(path, f"スケジュール [{i + 1}/{len(event.scenes)}]")
                elif isinstance(event, _WatcherEvent):
                    w = event.watcher
                    watcher_state.pause()
                    watcher_state.drain()
                    try:
                        run_scene(w.handler, f"watcher:{w.id}")
                    finally:
                        watcher_state.mark_fired(w.id)
                        watcher_state.resume()
                    if w.after == "stop":
                        log(f"watcher [{w.title or w.id}] after=stop のため終了")
                        return
                    if w.after == "restart_scene" and schedule_only:
                        if last_running_scene:
                            pol = scene_policies.get(last_running_scene, {})
                            policy = pol.get("policy", "always")
                            if policy == "once":
                                log(f"  → restart_scene: [{last_running_scene}] は1回限り設定のため復帰スキップ")
                            elif policy == "window":
                                started = pol.get("started_at") or datetime.now()
                                wmin = int(pol.get("window_min", 0))
                                elapsed_min = (datetime.now() - started).total_seconds() / 60.0
                                if elapsed_min > wmin:
                                    log(
                                        f"  → restart_scene: [{last_running_scene}] は枠超過 "
                                        f"({elapsed_min:.1f}/{wmin}分) のため復帰スキップ"
                                    )
                                else:
                                    log(
                                        f"  → restart_scene: [{last_running_scene}] を最初からやり直し "
                                        f"(枠内 {elapsed_min:.1f}/{wmin}分)"
                                    )
                                    run_scene(last_running_scene, "restart_scene")
                            else:
                                log(f"  → restart_scene: [{last_running_scene}] を最初からやり直し")
                                run_scene(last_running_scene, "restart_scene")
                        else:
                            # まだシーンが実行されていない — 直近スケジュールに戻る
                            fb = _last_due_scenes(flow, datetime.now())
                            if fb:
                                fb_entry, fallback = fb
                                names = ", ".join(fallback)
                                log(f"  → restart_scene: 未実行のため直近スケジュール [{names}] を実行")
                                fb_started = _entry_started_at(fb_entry, datetime.now())
                                for fi, fpath in enumerate(fallback):
                                    if should_stop():
                                        break
                                    scene_policies[fpath] = {
                                        "policy": fb_entry.retry_policy,
                                        "window_min": fb_entry.retry_window_min,
                                        "started_at": fb_started,
                                    }
                                    last_running_scene = fpath
                                    run_scene(fpath, f"restart_scene 直近スケジュール [{fi + 1}/{len(fallback)}]")
                            else:
                                log("  → restart_scene: 直前のシーンが不明のためスキップ")
                    elif w.after == "next_scene":
                        current_idx += 1
                    elif w.after == "noop":
                        log("  → noop: 何もせず待機に戻る")
                    # main_sequence の restart_scene: current_idx そのまま
                continue

            # 2. スケジュール直接評価（シーン開始前）
            fired = _check_schedule(flow, datetime.now(), last_fired_schedule)
            if fired is not None:
                idx, entry = fired
                last_fired_schedule[idx] = datetime.now().date()
                scenes = _entry_scenes(entry)
                log(f"📅 スケジュール発火: {entry.time} → {scenes}")
                pending.append(_ScheduleEvent(
                    scenes=scenes,
                    retry_policy=entry.retry_policy,
                    retry_window_min=entry.retry_window_min,
                    started_at=_entry_started_at(entry, datetime.now()),
                ))
                continue

            # 3. ウォッチャー直接評価（シーン開始前）
            w = watcher_state.pop_fired()
            if w is not None:
                log(f"👁 watcher 発火: [{w.title or w.id}] → {w.handler}")
                if w.alert_desktop and notify_fn:
                    notify_fn(f"ウォッチャー発火: {w.title}", w.handler or "")
                pending.append(_WatcherEvent(watcher=w))
                continue

            # 4. 通常メインシーケンス（main_sequence がある場合のみ）
            if not schedule_only:
                if current_idx < len(main_seq):
                    path = main_seq[current_idx]
                    last_running_scene = path
                    pending_before = len(pending)
                    run_scene(path, f"メイン [{current_idx + 1}/{len(main_seq)}]")
                    interrupted = len(pending) > pending_before
                    if not interrupted:
                        current_idx += 1
                    continue

                # 5. after_main
                if flow.after_main == "stop":
                    log("after_main=stop のため終了")
                    return
                # stay: 最後のシーンを繰り返す
                last_path = main_seq[-1]
                run_scene(last_path, "stay（最後のシーンを繰り返し）")
                continue

            # スケジュールのみモード: ポーリング待機
            poll = flow.settings.polling_interval_s if flow.settings else 1.0
            for _ in range(max(1, int(poll * 10))):
                if should_stop():
                    break
                time.sleep(0.1)
    finally:
        watcher_state.stop()
        log("停止")
