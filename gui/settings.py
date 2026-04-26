"""アプリ設定（デバイス一覧など）の永続化。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

SETTINGS_PATH = "settings.json"


@dataclass
class Device:
    label: str
    ip: str


@dataclass
class RecordingSettings:
    out_dir: str = "recordings"
    interval_min: int = 5
    jpeg_quality: int = 85
    auto_stop_enabled: bool = False
    auto_stop_time: str = "08:00"   # "HH:MM"
    auto_delete_enabled: bool = True
    auto_delete_days: int = 7


@dataclass
class AppSettings:
    devices: list[Device] = field(default_factory=list)
    tesseract_cmd: str = ""   # 空 = PATH から自動検出
    last_device: str = ""     # 最後に接続成功したデバイスの IP / USB シリアル
    last_flow: str = ""       # 最後に開いたフローのパス
    recording: RecordingSettings = field(default_factory=RecordingSettings)


def _default_settings() -> AppSettings:
    return AppSettings(devices=[
        Device(label="オフィス", ip="192.168.255.57"),
        Device(label="自宅",     ip="192.168.0.119"),
    ])


def _parse_device(d: dict) -> Device | None:
    """新旧全フォーマットに対応:
    - 最新: {"label", "ip"}
    - 旧A : {"label", "ip", "port"}              → port を捨てる
    - 旧B : {"label", "serial": "IP:PORT"}       → IP だけ取り出す
    """
    label = d.get("label", "").strip()
    if not label:
        return None
    if "ip" in d:
        ip = str(d["ip"]).strip()
    elif "serial" in d:
        s = str(d["serial"]).strip()
        ip = s.rsplit(":", 1)[0] if ":" in s else s
    else:
        return None
    if not ip:
        return None
    return Device(label=label, ip=ip)


def load_settings(path: str = SETTINGS_PATH) -> AppSettings:
    if not os.path.exists(path):
        s = _default_settings()
        save_settings(s, path)
        return s
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    devices: list[Device] = []
    for d in data.get("devices", []):
        dev = _parse_device(d)
        if dev:
            devices.append(dev)
    rec_data = data.get("recording", {}) or {}
    defaults = RecordingSettings()
    recording = RecordingSettings(
        out_dir=rec_data.get("out_dir", defaults.out_dir),
        interval_min=int(rec_data.get("interval_min", defaults.interval_min)),
        jpeg_quality=int(rec_data.get("jpeg_quality", defaults.jpeg_quality)),
        auto_stop_enabled=bool(rec_data.get("auto_stop_enabled", defaults.auto_stop_enabled)),
        auto_stop_time=rec_data.get("auto_stop_time", defaults.auto_stop_time),
        auto_delete_enabled=bool(rec_data.get("auto_delete_enabled", defaults.auto_delete_enabled)),
        auto_delete_days=int(rec_data.get("auto_delete_days", defaults.auto_delete_days)),
    )
    return AppSettings(
        devices=devices,
        tesseract_cmd=data.get("tesseract_cmd", ""),
        last_device=data.get("last_device", ""),
        last_flow=data.get("last_flow", ""),
        recording=recording,
    )


def save_settings(s: AppSettings, path: str = SETTINGS_PATH) -> None:
    r = s.recording
    data = {
        "devices": [{"label": d.label, "ip": d.ip} for d in s.devices],
        "tesseract_cmd": s.tesseract_cmd,
        "last_device": s.last_device,
        "last_flow": s.last_flow,
        "recording": {
            "out_dir": r.out_dir,
            "interval_min": r.interval_min,
            "jpeg_quality": r.jpeg_quality,
            "auto_stop_enabled": r.auto_stop_enabled,
            "auto_stop_time": r.auto_stop_time,
            "auto_delete_enabled": r.auto_delete_enabled,
            "auto_delete_days": r.auto_delete_days,
        },
    }
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
