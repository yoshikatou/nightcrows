"""
記録したタップを再生する

使用方法:
    python tests/tap_replay.py recordings/session1.json
    python tests/tap_replay.py recordings/session1.json --serial 192.168.0.119:35103

環境変数 ADB_SERIAL でデフォルトシリアルを指定可能。
JSON の "serial" は参考情報として表示するのみで、実際の送信先は --serial 引数を優先する。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time

ADB = r"C:\scrcpy\adb.exe"
DEFAULT_SERIAL = os.environ.get("ADB_SERIAL", "192.168.255.57:34497")


def replay(serial: str, input_path: str) -> None:
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    events = data.get("events", [])
    recorded_on = data.get("serial", "?")
    print(f"再生開始: target_serial={serial} (記録時={recorded_on})")
    print(f"入力   : {input_path} ({len(events)} 件)")
    print("-" * 48)

    start = time.monotonic()
    for i, ev in enumerate(events):
        target = start + ev["t"]
        wait = target - time.monotonic()
        if wait > 0:
            time.sleep(wait)

        x1, y1, x2, y2 = ev["x1"], ev["y1"], ev["x2"], ev["y2"]
        d = ev["duration_ms"]
        kind = "TAP  " if (abs(x2 - x1) < 10 and abs(y2 - y1) < 10) else "SWIPE"
        print(f"[{ev['t']:7.2f}s] {kind} {i+1:3d}/{len(events)}: "
              f"({x1:4d},{y1:4d})->({x2:4d},{y2:4d}) {d}ms")
        subprocess.run(
            [ADB, "-s", serial, "shell", "input", "swipe",
             str(x1), str(y1), str(x2), str(y2), str(d)],
            capture_output=True, timeout=d // 1000 + 5,
        )

    print("完了")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("input", help="入力 JSON パス")
    p.add_argument("--serial", default=DEFAULT_SERIAL,
                   help=f"ADB シリアル (default: {DEFAULT_SERIAL})")
    args = p.parse_args()
    replay(args.serial, args.input)


if __name__ == "__main__":
    main()
