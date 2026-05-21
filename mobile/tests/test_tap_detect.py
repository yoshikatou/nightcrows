"""
人間のタップ座標をリアルタイムで検出するテスト

使用方法:
    python tests/test_tap_detect.py

スマホをタップすると座標が表示される。Ctrl+C で終了。
"""
import subprocess
import sys
import re

SERIAL = "192.168.255.57:34497"
ADB    = r"C:\scrcpy\adb.exe"

# タッチデバイス（ABS_MT_POSITION_X を持つもの）
TOUCH_DEVICE = "/dev/input/event3"

# スクリーン解像度（getevent 座標 → 論理座標への変換用）
# getevent の max: X=12200, Y=27120 → 物理解像度 1220x2712 の10倍
SCALE = 10


def detect_taps():
    print("タップ検出中... (Ctrl+C で終了)")
    print(f"デバイス: {TOUCH_DEVICE}")
    print("-" * 40)

    cmd = [ADB, "-s", SERIAL, "shell", "getevent", "-l", TOUCH_DEVICE]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    x = y = None
    tracking_id = None

    try:
        for line in proc.stdout:
            txt = line.decode(errors="replace").strip()

            if "ABS_MT_POSITION_X" in txt:
                m = re.search(r"([0-9a-f]+)$", txt)
                if m:
                    x = int(m.group(1), 16) // SCALE

            elif "ABS_MT_POSITION_Y" in txt:
                m = re.search(r"([0-9a-f]+)$", txt)
                if m:
                    y = int(m.group(1), 16) // SCALE

            elif "ABS_MT_TRACKING_ID" in txt:
                m = re.search(r"([0-9a-f]+)$", txt)
                if m:
                    val = int(m.group(1), 16)
                    if val == 0xffffffff:
                        # 指が離れた
                        if x is not None and y is not None:
                            print(f"タップ: ({x}, {y})")
                        x = y = None
                    else:
                        tracking_id = val

    except KeyboardInterrupt:
        print("\n終了")
    finally:
        proc.terminate()


if __name__ == "__main__":
    detect_taps()
