"""ウォッチャーごとの日次発火カウントを state/watcher_counts.json に永続化する。

スキーマ:
    {
        "date": "2026-05-25",
        "counts": {"<watcher_id>": 12, ...},
        "last_fired": {"<watcher_id>": "14:32:10", ...}
    }

日付が変わったら自動的に counts/last_fired をリセットする（深夜 0 時境界）。
保存はファイル単位でロックを取り、別スレッドからの同時呼び出しを直列化する。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

STATE_DIR = "state"
COUNTS_FILE = os.path.join(STATE_DIR, "watcher_counts.json")

_lock = threading.Lock()


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _empty(date_str: str | None = None) -> dict:
    return {
        "date": date_str or _today_str(),
        "counts": {},
        "last_fired": {},
    }


def _read_raw() -> dict:
    if not os.path.exists(COUNTS_FILE):
        return _empty()
    try:
        with open(COUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("date", _today_str())
    data.setdefault("counts", {})
    data.setdefault("last_fired", {})
    if not isinstance(data["counts"], dict):
        data["counts"] = {}
    if not isinstance(data["last_fired"], dict):
        data["last_fired"] = {}
    return data


def _write_raw(data: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = COUNTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, COUNTS_FILE)


def _maybe_rollover(data: dict) -> dict:
    today = _today_str()
    if data.get("date") != today:
        data["date"] = today
        data["counts"] = {}
        data["last_fired"] = {}
    return data


def load_counts() -> dict:
    """現在のカウントを読み込む（日付が変わっていればリセットして保存）。"""
    with _lock:
        data = _read_raw()
        before = data.get("date")
        data = _maybe_rollover(data)
        if before != data["date"]:
            try:
                _write_raw(data)
            except Exception:
                pass
        return {
            "date": data["date"],
            "counts": dict(data["counts"]),
            "last_fired": dict(data["last_fired"]),
        }


def record_fire(watcher_id: str) -> tuple[int, str]:
    """発火を 1 件記録し、(更新後カウント, 最終発火 HH:MM:SS) を返す。

    呼び出し時点で日付が変わっていれば全カウントをリセットしてから記録する。
    """
    if not watcher_id:
        return 0, ""
    with _lock:
        data = _read_raw()
        data = _maybe_rollover(data)
        counts = data["counts"]
        last = data["last_fired"]
        new_count = int(counts.get(watcher_id, 0)) + 1
        now_str = datetime.now().strftime("%H:%M:%S")
        counts[watcher_id] = new_count
        last[watcher_id] = now_str
        try:
            _write_raw(data)
        except Exception:
            pass
        return new_count, now_str
