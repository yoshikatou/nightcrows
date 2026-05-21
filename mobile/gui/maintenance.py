"""メンテナンス日程のデータモデルと入出力。"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime

MAINTENANCE_FILE = "maintenance.json"


@dataclass
class MaintenanceEntry:
    id: str = ""
    label: str = ""
    start: str = ""   # "YYYY-MM-DD HH:MM"
    end: str = ""     # "YYYY-MM-DD HH:MM"


def is_in_maintenance(
    entries: list[MaintenanceEntry], now: datetime
) -> MaintenanceEntry | None:
    """now がいずれかのメンテ窓に入っていれば該当エントリを返す。"""
    for e in entries:
        try:
            s = datetime.strptime(e.start, "%Y-%m-%d %H:%M")
            t = datetime.strptime(e.end, "%Y-%m-%d %H:%M")
            if s <= now < t:
                return e
        except ValueError:
            continue
    return None


def new_entry(label: str, start: str, end: str) -> MaintenanceEntry:
    return MaintenanceEntry(id=str(uuid.uuid4()), label=label, start=start, end=end)


def load_maintenance() -> list[MaintenanceEntry]:
    if not os.path.exists(MAINTENANCE_FILE):
        return []
    try:
        with open(MAINTENANCE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [
            MaintenanceEntry(
                id=d.get("id", str(uuid.uuid4())),
                label=d.get("label", ""),
                start=d.get("start", ""),
                end=d.get("end", ""),
            )
            for d in data
        ]
    except Exception:
        return []


def save_maintenance(entries: list[MaintenanceEntry]) -> None:
    with open(MAINTENANCE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            [{"id": e.id, "label": e.label, "start": e.start, "end": e.end}
             for e in entries],
            f, indent=2, ensure_ascii=False,
        )
