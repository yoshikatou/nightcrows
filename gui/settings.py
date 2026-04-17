"""アプリ設定（デバイス一覧など）の永続化。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

SETTINGS_PATH = "settings.json"


@dataclass
class Device:
    label: str
    serial: str


@dataclass
class AppSettings:
    devices: list[Device] = field(default_factory=list)


def _default_settings() -> AppSettings:
    return AppSettings(devices=[
        Device(label="オフィス", serial="192.168.255.57:34497"),
        Device(label="自宅",     serial="192.168.0.119:35103"),
    ])


def load_settings(path: str = SETTINGS_PATH) -> AppSettings:
    if not os.path.exists(path):
        s = _default_settings()
        save_settings(s, path)
        return s
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    devices = [Device(label=d["label"], serial=d["serial"])
               for d in data.get("devices", [])]
    return AppSettings(devices=devices)


def save_settings(s: AppSettings, path: str = SETTINGS_PATH) -> None:
    data = {"devices": [{"label": d.label, "serial": d.serial} for d in s.devices]}
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
