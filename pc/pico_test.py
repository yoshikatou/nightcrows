r"""Pico HID マウスによるクリック動作確認用テスト。

実行すると 5 秒カウントダウン後、その時点のマウスカーソル位置に
Pico HID 経由で左クリックを 1 回送る。

事前準備:
  1. Pico に CircuitPython をインストール
     https://circuitpython.org/board/raspberry_pi_pico/
  2. CIRCUITPY ドライブの /lib に adafruit_hid をコピー
     https://github.com/adafruit/Adafruit_CircuitPython_HID/releases
  3. pc/pico/code.py を CIRCUITPY/code.py にコピー
  4. Pico を USB 接続して REPL が出るまで待つ
  5. pip install pyserial

実行: ..\.venv\Scripts\python.exe pico_test.py [COMポート]
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from pc.pico_mouse import PicoMouse, list_pico_ports, find_pico_port


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else None

    print("=== Pico HID マウステスト ===")

    # ポート一覧を表示
    ports = list_pico_ports()
    if ports:
        print(f"検出した Pico デバイス:")
        for p, desc in ports:
            print(f"  {p}: {desc}")
    else:
        auto = find_pico_port()
        if auto is None:
            print("  Pico が見つかりません。USB 接続を確認してください")
            if port is None:
                return
        print(f"  (自動検出: {auto})")

    resolved = port or find_pico_port()
    print(f"\n接続先: {resolved}")

    try:
        mouse = PicoMouse(resolved)
    except RuntimeError as e:
        print(f"初期化失敗: {e}")
        return

    print(f"Pico 接続 OK (port={mouse.port})")
    print()
    print("反応を見たいボタン上にマウスカーソルを置いてください")
    print("（5秒後にその位置を左クリック。対象ウィンドウをフォアグラウンドに）")
    print()

    for i in range(5, 0, -1):
        x, y = mouse.get_cursor_pos()
        print(f"  {i} ... (現在カーソル: {x}, {y})")
        time.sleep(1)

    x, y = mouse.get_cursor_pos()
    print(f"\nクリック実行: ({x}, {y})")

    try:
        mouse.click(x, y)
        print("送信完了。対象側でクリックが反応したか確認してください。")
    except RuntimeError as e:
        print(f"クリック失敗: {e}")
    finally:
        mouse.close()


if __name__ == "__main__":
    main()
