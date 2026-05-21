"""pc/settings.json の読み書き。"""
from __future__ import annotations

import json
import os

SETTINGS_PATH = "settings.json"
EXP_METER_PATH = "exp_meter.json"

_DEFAULTS = {
    "window_title": "",
    "tesseract_cmd": "",     # 空欄 = 自動検出
    "overlay_pos": None,     # [x, y] or None (= 右上)
    "log_retain_days": 30,   # この日数より古いログは起動時に自動削除（1〜365）
}


def load_settings() -> dict:
    if not os.path.exists(SETTINGS_PATH):
        return dict(_DEFAULTS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        merged = dict(_DEFAULTS)
        merged.update(d)
        return merged
    except Exception:
        return dict(_DEFAULTS)


def save_settings(d: dict) -> None:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
