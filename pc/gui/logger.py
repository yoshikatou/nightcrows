"""共通ログユーティリティ（logs/YYYY-MM-DD.log への追記 + ローテーション）。

PC 全モジュールで共通の保存先・形式を使うことで、フロー実行・ウォッチャー発火・
経験値メーターのログを 1 つのファイルに統合できる。

使い方:
    from .logger import write_log
    write_log("🔥 ウォッチャー発火: [体力低下] score=0.92")

行フォーマット: `[HH:MM:SS] {msg}\n`
保存先:         `logs/YYYY-MM-DD.log` （CWD 配下、起動時に pc/ に固定済み）
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

LOG_DIR = "logs"
DEFAULT_LOG_RETAIN_DAYS = 30


def write_log(msg: str) -> None:
    """logs/YYYY-MM-DD.log に行追記する。例外は握りつぶす（ログのために本処理を落とさない）。"""
    now = datetime.now()
    line = f"[{now.strftime('%H:%M:%S')}] {msg}\n"
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f"{now.strftime('%Y-%m-%d')}.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def purge_old_logs(retain_days: int = DEFAULT_LOG_RETAIN_DAYS) -> int:
    """retain_days より古い .log ファイルを削除し、削除数を返す。"""
    if not os.path.isdir(LOG_DIR):
        return 0
    removed = 0
    try:
        cutoff = datetime.now() - timedelta(days=max(1, retain_days))
        for fname in os.listdir(LOG_DIR):
            if not fname.endswith(".log"):
                continue
            stem = fname[:-4]
            try:
                file_date = datetime.strptime(stem, "%Y-%m-%d")
            except ValueError:
                continue
            if file_date < cutoff:
                try:
                    os.remove(os.path.join(LOG_DIR, fname))
                    removed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return removed
