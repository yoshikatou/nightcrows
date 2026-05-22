r"""シーン動作テスト。

指定座標をクリック後、マウスカーソルを中央→左→中央→右と動かす。
カメラ左右スイープ操作の確認用。

実行: ..\.venv\Scripts\python.exe scene_test.py [COMポート]
"""
from __future__ import annotations

import sys
import time
import ctypes
from ctypes import wintypes

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from pc.pico_mouse import PicoMouse, find_pico_port

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]


def screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def countdown(sec: int) -> None:
    for i in range(sec, 0, -1):
        print(f"  {i}...")
        time.sleep(1)
    print()


def run_scene(
    mouse: PicoMouse,
    click_x: int,
    click_y: int,
    sweep_cx: int,
    sweep_cy: int,
    sweep_px: int = 300,
    sweep_button: str = "R",
    max_step: int = 15,
    delay: float = 0.015,
) -> None:
    """
    1. (click_x, click_y) を左クリック
    2. カーソルをスイープ中心に移動
    3. sweep_button を押したまま 左 → 中央 → 右 → 中央 とスイープ
    4. ボタンを離す

    sweep_button="L" にすれば左ボタンドラッグ。
    ドラッグ不要なら sweep_button="" で移動のみ（未対応、"R"推奨）。
    """
    print(f"[1] クリック ({click_x}, {click_y})")
    mouse.click(click_x, click_y)
    time.sleep(0.3)

    print(f"[2] スイープ中心へ移動 ({sweep_cx}, {sweep_cy})")
    mouse.move_cursor(sweep_cx, sweep_cy)
    time.sleep(0.2)

    left_x  = sweep_cx - sweep_px
    right_x = sweep_cx + sweep_px

    print(f"[3] {sweep_button}ボタン押し下げ")
    mouse.press(sweep_button)
    time.sleep(0.1)

    print(f"[4] 左へ ({sweep_cx} → {left_x})")
    mouse.move_to(left_x, sweep_cy, max_step=max_step, delay=delay)

    print(f"[5] 中央へ ({left_x} → {sweep_cx})")
    mouse.move_to(sweep_cx, sweep_cy, max_step=max_step, delay=delay)

    print(f"[6] 右へ ({sweep_cx} → {right_x})")
    mouse.move_to(right_x, sweep_cy, max_step=max_step, delay=delay)

    print(f"[7] 中央へ ({right_x} → {sweep_cx})")
    mouse.move_to(sweep_cx, sweep_cy, max_step=max_step, delay=delay)

    print(f"[8] {sweep_button}ボタン解放")
    mouse.release(sweep_button)
    print("完了\n")


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else find_pico_port()
    if port is None:
        print("Pico が見つかりません")
        return

    print(f"=== シーンテスト (port={port}) ===")
    try:
        mouse = PicoMouse(port)
    except RuntimeError as e:
        print(f"初期化失敗: {e}")
        return

    w, h = screen_size()
    cx, cy = w // 2, h // 2
    print(f"接続 OK  画面: {w}x{h}  中央: ({cx}, {cy})\n")

    print("キャリブレーション中...")
    scale = mouse.calibrate()
    print(f"スケール: {scale:.3f}\n")

    while True:
        print("---- シーン設定 ----")
        try:
            raw = input(f"  クリック座標 x y (空Enter={cx} {cy}): ").strip().split()
            if raw:
                click_x, click_y = int(raw[0]), int(raw[1])
            else:
                click_x, click_y = cx, cy

            raw = input(f"  スイープ中心 x y (空Enter={cx} {cy}): ").strip().split()
            if raw:
                sweep_cx, sweep_cy = int(raw[0]), int(raw[1])
            else:
                sweep_cx, sweep_cy = cx, cy

            raw = input("  スイープ幅px (デフォルト300): ").strip()
            sweep_px = int(raw) if raw else 300

            raw = input("  スイープボタン L/R/M (デフォルトR): ").strip().upper()
            sweep_button = raw if raw in ("L", "R", "M") else "R"

        except (ValueError, KeyboardInterrupt):
            print("\n終了")
            break

        print()
        print(f"5秒後に開始。ゲームウィンドウをフォアグラウンドにしてください")
        countdown(5)

        try:
            run_scene(
                mouse,
                click_x=click_x,
                click_y=click_y,
                sweep_cx=sweep_cx,
                sweep_cy=sweep_cy,
                sweep_px=sweep_px,
                sweep_button=sweep_button,
            )
        except RuntimeError as e:
            print(f"エラー: {e}\n")

        again = input("もう一度? (y/Enter=yes, n=終了): ").strip().lower()
        if again == "n":
            break
        print()

    mouse.close()
    print("終了")


if __name__ == "__main__":
    main()
