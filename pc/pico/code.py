"""
Pico HID マウスコントローラ (CircuitPython 用)

CIRCUITPY ドライブの code.py として配置する。
USB CDC シリアル経由でコマンドを受信し、HID マウス操作を実行する。

コマンド形式 (改行区切り):
    CLICK L [hold_ms]   左クリック (デフォルト 30ms)
    CLICK R [hold_ms]   右クリック
    CLICK M [hold_ms]   中クリック
    MOVE dx dy          相対移動 (-127〜127)
    PING                疎通確認

レスポンス:
    OK      成功
    ERROR   失敗
"""
import time
import sys
import usb_hid
from adafruit_hid.mouse import Mouse

mouse = Mouse(usb_hid.devices)
print("Pico HID Mouse ready")

while True:
    line = sys.stdin.readline()
    if not line:
        continue

    parts = line.strip().split()
    if not parts:
        print("OK")
        continue

    cmd = parts[0].upper()

    if cmd == "PING":
        print("OK")

    elif cmd == "CLICK":
        btn_char = parts[1].upper() if len(parts) > 1 else "L"
        hold_ms = int(parts[2]) if len(parts) > 2 else 30
        btn_map = {
            "L": Mouse.LEFT_BUTTON,
            "R": Mouse.RIGHT_BUTTON,
            "M": Mouse.MIDDLE_BUTTON,
        }
        btn = btn_map.get(btn_char)
        if btn is None:
            print("ERROR unknown button")
            continue
        mouse.press(btn)
        time.sleep(hold_ms / 1000)
        mouse.release(btn)
        print("OK")

    elif cmd == "MOVE":
        if len(parts) < 3:
            print("ERROR MOVE requires dx dy")
            continue
        dx = max(-127, min(127, int(parts[1])))
        dy = max(-127, min(127, int(parts[2])))
        mouse.move(dx, dy)
        print("OK")

    else:
        print(f"ERROR unknown command: {cmd}")
