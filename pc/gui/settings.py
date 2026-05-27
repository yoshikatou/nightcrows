"""pc/settings.json の読み書き。"""
from __future__ import annotations

import json
import os

SETTINGS_PATH = "settings.json"
EXP_METER_PATH = "exp_meter.json"

_DEFAULTS = {
    "window_title": "",
    "tesseract_cmd": "",        # 空欄 = 自動検出
    "overlay_pos": None,        # [x, y] or None (= 右上)
    "log_retain_days": 30,      # この日数より古いログは起動時に自動削除（1〜365）
    "google_chat_webhook": "",  # 空欄 = 通知無効。Google Chat の Incoming Webhook URL
    # 前面化確認ダイアログの再表示間隔（分）。
    # ウォッチャー/スケジュール発火で対象ウィンドウが前面でない時、
    # この間隔中に出した選択 (run / skip) をキャッシュして自動適用する。
    # 0 = 毎回確認（キャッシュなし）、5 = 5分に 1 回だけ表示。
    "foreground_check_interval_min": 5.0,
    # --- フロー実行中オーバーレイ ---
    "flow_overlay_enabled": True,         # フロー開始時にオーバーレイを自動表示
    "flow_overlay_pos": None,             # [x, y] or None (= 左上付近)
    # --- 翻訳タブ ---
    "translation_api_key": "",            # 空欄 = 翻訳機能無効
    "translation_base_lang": "ja",        # チャット翻訳の出力ベース言語
    "translation_region": None,           # [rx, ry, rw, rh] (0.0〜1.0) or None
    "translation_interval_s": 5.0,        # 領域監視の間隔（秒）
    "translation_user_targets": ["en"],   # ユーザー入力翻訳の対象言語コード列
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
