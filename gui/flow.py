"""フロー（シーン間の繋ぎ方）のデータモデルと JSON 入出力。

Flow は「複数シーンをどう繋いで再生するか」を定義する上位レイヤー。
- main_sequence: 順次再生するシーンの並び
- schedule:      時刻トリガ（毎日 / 1回だけ）
- watchers:      常時監視ルール（画像出現 / 画像消失 / 数字閾値）
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any


# ------------------------------------------------------------- schedule entry
@dataclass
class ScheduleEntry:
    time: str = "00:00"              # "HH:MM"
    target: str = ""                 # scenes/ からの相対パス
    repeat: str = "daily"            # "daily" | "weekly" | "once"
    days: list[int] = field(default_factory=list)  # 0=月〜6=日  repeat="weekly" のとき使用
    date: str = ""                   # "YYYY-MM-DD"  repeat="once" のとき使用


# -------------------------------------------------------------- watcher types
@dataclass
class Condition:
    type: str = "image_appear"       # "image_appear" | "image_gone" | "digit_threshold" | "ocr_number"
    # 画像系
    template: str = ""               # image_appear / image_gone のテンプレ画像
    region: list[int] = field(default_factory=list)  # [x, y, w, h]
    threshold: float = 0.85
    consecutive: int = 3             # image_gone のとき N 回連続マッチしなければ発火
    # 数字系 (digit_threshold)
    digits_dir: str = ""             # 0.png〜9.png が入ったディレクトリ
    op: str = "<="                   # "<" "<=" ">" ">=" "=="
    value: int = 0
    # OCR 系 (ocr_number) — Tesseract で数値を読む
    ocr_whitelist: str = "0123456789"  # 読み取る文字種


@dataclass
class Watcher:
    id: str = ""
    enabled: bool = True
    priority: int = 0
    condition: Condition = field(default_factory=Condition)
    handler: str = ""                # scenes/ からの相対パス
    after: str = "restart_scene"     # "restart_scene" | "next_scene" | "stop"
    cooldown_s: float = 0.0
    interrupt: str = "step_end"      # "step_end" | "immediate"


# ----------------------------------------------------------------------- flow
@dataclass
class FlowSettings:
    polling_interval_s: float = 1.0


@dataclass
class Flow:
    name: str = "untitled"
    version: int = 1
    device_ip: str = ""
    main_sequence: list[str] = field(default_factory=list)
    after_main: str = "stay"         # "stay" | "stop"
    schedule: list[ScheduleEntry] = field(default_factory=list)
    watchers: list[Watcher] = field(default_factory=list)
    settings: FlowSettings = field(default_factory=FlowSettings)


# ----------------------------------------------------------------- (de)serialize
def _cond_to_dict(c: Condition) -> dict[str, Any]:
    d: dict[str, Any] = {"type": c.type}
    if c.type in ("image_appear", "image_gone"):
        d["template"] = c.template
        if c.region:
            d["region"] = list(c.region)
        d["threshold"] = c.threshold
        if c.type == "image_gone":
            d["consecutive"] = c.consecutive
    elif c.type == "digit_threshold":
        if c.region:
            d["region"] = list(c.region)
        d["digits_dir"] = c.digits_dir
        d["op"] = c.op
        d["value"] = c.value
    elif c.type == "ocr_number":
        if c.region:
            d["region"] = list(c.region)
        d["ocr_whitelist"] = c.ocr_whitelist
        d["op"] = c.op
        d["value"] = c.value
    return d


def _cond_from_dict(d: dict[str, Any]) -> Condition:
    t = d.get("type", "image_appear")
    return Condition(
        type=t,
        template=d.get("template", ""),
        region=list(d.get("region", []) or []),
        threshold=float(d.get("threshold", 0.85)),
        consecutive=int(d.get("consecutive", 3)),
        digits_dir=d.get("digits_dir", ""),
        op=d.get("op", "<="),
        value=int(d.get("value", 0)),
        ocr_whitelist=d.get("ocr_whitelist", "0123456789"),
    )


def _watcher_to_dict(w: Watcher) -> dict[str, Any]:
    return {
        "id": w.id,
        "enabled": w.enabled,
        "priority": w.priority,
        "condition": _cond_to_dict(w.condition),
        "handler": w.handler,
        "after": w.after,
        "cooldown_s": w.cooldown_s,
        "interrupt": w.interrupt,
    }


def _watcher_from_dict(d: dict[str, Any]) -> Watcher:
    return Watcher(
        id=d.get("id", ""),
        enabled=bool(d.get("enabled", True)),
        priority=int(d.get("priority", 0)),
        condition=_cond_from_dict(d.get("condition", {})),
        handler=d.get("handler", ""),
        after=d.get("after", "restart_scene"),
        cooldown_s=float(d.get("cooldown_s", 0.0)),
        interrupt=d.get("interrupt", "step_end"),
    )


def _schedule_to_dict(s: ScheduleEntry) -> dict[str, Any]:
    d: dict[str, Any] = {
        "time": s.time,
        "target": s.target,
        "repeat": s.repeat,
    }
    if s.repeat == "weekly" and s.days:
        d["days"] = list(s.days)
    if s.repeat == "once" and s.date:
        d["date"] = s.date
    return d


def _schedule_from_dict(d: dict[str, Any]) -> ScheduleEntry:
    return ScheduleEntry(
        time=d.get("time", "00:00"),
        target=d.get("target", ""),
        repeat=d.get("repeat", "daily"),
        days=list(d.get("days", []) or []),
        date=d.get("date", ""),
    )


def save_watchers(watchers: list[Watcher], path: str) -> None:
    """ウォッチャーリストをスタンドアロン JSON に保存。"""
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([_watcher_to_dict(w) for w in watchers], f,
                  indent=2, ensure_ascii=False)


def load_watchers(path: str) -> list[Watcher]:
    """スタンドアロン JSON からウォッチャーリストを読み込む。"""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [_watcher_from_dict(d) for d in (data or [])]


def save_flow(flow: Flow, path: str) -> None:
    data = {
        "name": flow.name,
        "version": flow.version,
        "device_ip": flow.device_ip,
        "main_sequence": list(flow.main_sequence),
        "after_main": flow.after_main,
        "schedule": [_schedule_to_dict(s) for s in flow.schedule],
        "watchers": [_watcher_to_dict(w) for w in flow.watchers],
        "settings": {"polling_interval_s": flow.settings.polling_interval_s},
    }
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_flow(path: str) -> Flow:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    settings_d = data.get("settings", {}) or {}
    return Flow(
        name=data.get("name", "untitled"),
        version=int(data.get("version", 1)),
        device_ip=data.get("device_ip", ""),
        main_sequence=list(data.get("main_sequence", []) or []),
        after_main=data.get("after_main", "stay"),
        schedule=[_schedule_from_dict(x) for x in data.get("schedule", []) or []],
        watchers=[_watcher_from_dict(x) for x in data.get("watchers", []) or []],
        settings=FlowSettings(
            polling_interval_s=float(settings_d.get("polling_interval_s", 1.0)),
        ),
    )
