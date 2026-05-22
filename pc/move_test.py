r"""Pico HID マウス移動の動作確認テスト。

カーソルを画面の各ポイントに Pico 相対移動で動かし、
実際の着地座標と誤差を表示する。

実行: ..\.venv\Scripts\python.exe move_test.py [COMポート]

メニュー:
  c. キャリブレーション
  1. 画面中央へ移動（精度確認）
  2. 四隅を順番に移動
  3. 座標指定移動（誤差自動補正あり）
  4. 現在位置クリック
  5. 四隅巡回（誤差自動補正あり）
  6. クリック座標取得（左クリック=記録、右クリック=終了）
  q. 終了
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

# マウスフック用定数・構造体
WH_MOUSE_LL  = 14
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt",          wintypes.POINT),
        ("mouseData",   wintypes.DWORD),
        ("flags",       wintypes.DWORD),
        ("time",        wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]

user32.SetWindowsHookExW.restype  = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype     = ctypes.c_long
user32.CallNextHookEx.argtypes    = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.UnhookWindowsHookEx.restype  = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.GetMessageW.restype  = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PostQuitMessage.restype  = None
user32.PostQuitMessage.argtypes = [ctypes.c_int]


def capture_click_coords() -> list[tuple[int, int]]:
    """左クリックした座標を記録する。右クリックで終了。"""
    coords: list[tuple[int, int]] = []

    def _hook(n_code: int, w_param: int, l_param: int) -> int:
        if n_code >= 0:
            if w_param == WM_LBUTTONDOWN:
                info = ctypes.cast(l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                x, y = info.pt.x, info.pt.y
                coords.append((x, y))
                print(f"  [{len(coords):2d}] ({x}, {y})")
            elif w_param == WM_RBUTTONDOWN:
                user32.PostQuitMessage(0)
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    fn   = _HOOKPROC(_hook)
    hook = user32.SetWindowsHookExW(WH_MOUSE_LL, fn, None, 0)
    msg  = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        pass  # WM_QUIT が来るまでブロック
    user32.UnhookWindowsHookEx(hook)
    return coords

SM_CXSCREEN = 0
SM_CYSCREEN = 1


def screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)


def countdown(sec: int) -> None:
    for i in range(sec, 0, -1):
        print(f"  {i}...")
        time.sleep(1)
    print()


def move_and_report(
    mouse: PicoMouse, tx: int, ty: int, label: str = "",
    max_step: int = 20, delay: float = 0.02,
) -> None:
    bx, by = mouse.get_cursor_pos()
    print(f"  [{label}] 目標: ({tx}, {ty})  出発: ({bx}, {by})")
    mouse.move_to(tx, ty, max_step=max_step, delay=delay)
    time.sleep(0.05)
    ax, ay = mouse.get_cursor_pos()
    ex, ey = ax - tx, ay - ty
    ok = "✓" if abs(ex) <= 2 and abs(ey) <= 2 else "△ 誤差あり"
    print(f"         到着: ({ax}, {ay})  誤差=({ex:+d}, {ey:+d}) {ok}")
    print()


def move_accurate_and_report(
    mouse: PicoMouse, tx: int, ty: int, label: str = "",
    step: int = 20, delay: float = 0.02, tolerance: int = 3,
) -> None:
    bx, by = mouse.get_cursor_pos()
    print(f"  [{label}] 目標: ({tx}, {ty})  出発: ({bx}, {by})")

    iterations: list[tuple[int, int, int, int, int]] = []

    def on_iter(i: int, cx: int, cy: int, ex: int, ey: int) -> None:
        iterations.append((i, cx, cy, ex, ey))
        tag = "✓" if abs(ex) <= tolerance and abs(ey) <= tolerance else f"補正{i+1}"
        print(f"         [{tag}] 実位置: ({cx}, {cy})  残差=({ex:+d}, {ey:+d})")

    ax, ay = mouse.move_to_accurate(
        tx, ty,
        tolerance=tolerance,
        step=step,
        delay=delay,
        on_iter=on_iter,
    )
    ex, ey = ax - tx, ay - ty
    ok = "✓ 収束" if abs(ex) <= tolerance and abs(ey) <= tolerance else "× 未収束"
    print(f"         最終: ({ax}, {ay})  誤差=({ex:+d}, {ey:+d}) {ok}  ({len(iterations)}回)\n")


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else find_pico_port()
    if port is None:
        print("Pico が見つかりません。COMポートを引数で指定してください")
        return

    print(f"=== Pico HID 移動テスト (port={port}) ===")
    try:
        mouse = PicoMouse(port)
    except RuntimeError as e:
        print(f"初期化失敗: {e}")
        return

    w, h = screen_size()
    print(f"接続 OK  画面サイズ: {w}x{h}")
    print()
    print("【キャリブレーション推奨】")
    print("  HID 移動の精度を上げるためにポインター速度を測定します。")
    print("  カーソルを画面中央付近に置いて Enter を押してください。")
    input("  準備ができたら Enter > ")
    scale = mouse.calibrate()
    print(f"  スケール係数: {scale:.3f}  (HID 1単位 = {scale:.2f}px)")
    if abs(scale - 1.0) > 0.05:
        print(f"  ポインター加速/速度の影響あり。補正を適用します。")
    else:
        print(f"  ほぼ 1:1 です。補正なしでも精度が出るはずです。")
    print()

    while True:
        cx, cy = mouse.get_cursor_pos()
        print(f"現在カーソル: ({cx}, {cy})")
        print(f"  スケール: {mouse._speed_scale:.3f}")
        print("  c: 再キャリブレーション")
        print("  1: 中央へ移動")
        print("  2: 四隅を巡回")
        print("  3: 座標指定移動（補正あり）")
        print("  4: 現在位置クリック")
        print("  5: 四隅巡回（補正あり）")
        print("  6: クリック座標取得  左クリック=記録 右クリック=終了")
        print("  7: 精度限界テスト（同一座標へ繰り返し移動して誤差を計測）")
        print("  8: シーンテスト（クリック → 左右スイープ）")
        print("  q: 終了")
        choice = input("> ").strip().lower()
        print()

        if choice == "q":
            break

        elif choice == "c":
            scale = mouse.calibrate()
            print(f"  再キャリブレーション完了: スケール={scale:.3f}\n")
            continue

        elif choice == "6":
            print("  左クリックした座標を記録します。右クリックで終了。\n")
            result = capture_click_coords()
            print(f"\n  記録した座標 ({len(result)}件):")
            for i, (x, y) in enumerate(result, 1):
                print(f"    [{i:2d}] ({x}, {y})")
            print()
            continue

        elif choice == "7":
            try:
                raw = input("  目標 x y (空Enterで中央): ").strip().split()
                if raw:
                    tx, ty = int(raw[0]), int(raw[1])
                else:
                    tx, ty = w // 2, h // 2
                reps_s = input("  繰り返し回数 (デフォルト10): ").strip()
                reps = int(reps_s) if reps_s else 10
            except (ValueError, IndexError):
                print("  入力エラー\n")
                continue

            print(f"\n5秒後に開始します。目標: ({tx}, {ty})  {reps}回")
            countdown(5)

            errors: list[tuple[int, int, int]] = []  # (iter数, |ex|, |ey|)
            for n in range(1, reps + 1):
                # 対角から移動して毎回同じ条件にする
                origin_x = tx + (300 if tx < w // 2 else -300)
                origin_y = ty + (200 if ty < h // 2 else -200)
                origin_x = max(0, min(w - 1, origin_x))
                origin_y = max(0, min(h - 1, origin_y))
                mouse.move_cursor(origin_x, origin_y)
                time.sleep(0.1)

                iters: list[tuple[int, int, int, int, int]] = []
                def _on_iter(i, cx, cy, ex, ey, _iters=iters):
                    _iters.append((i, cx, cy, ex, ey))

                ax, ay = mouse.move_to_accurate(tx, ty, on_iter=_on_iter)
                ex, ey = ax - tx, ay - ty
                dist = max(abs(ex), abs(ey))
                ok = "✓" if dist <= 3 else "△"
                print(f"  [{n:2d}] 誤差=({ex:+d},{ey:+d}) max={dist}px {ok} ({len(iters)}回補正)")
                errors.append((len(iters), abs(ex), abs(ey)))
                time.sleep(0.2)

            # 統計
            print()
            max_errs = [max(ex, ey) for _, ex, ey in errors]
            iters_list = [it for it, _, _ in errors]
            print(f"  === 統計 ({reps}回) ===")
            print(f"  誤差 max={max(max_errs)}px  avg={sum(max_errs)/len(max_errs):.1f}px  min={min(max_errs)}px")
            print(f"  補正 max={max(iters_list)}回  avg={sum(iters_list)/len(iters_list):.1f}回  min={min(iters_list)}回")
            print(f"  速度スケール: {mouse._speed_scale:.3f}  理論最小誤差: ±{mouse._speed_scale/2:.1f}px")
            print()
            continue

        elif choice == "8":
            try:
                raw = input("  クリック座標 x y: ").strip().split()
                click_x, click_y = int(raw[0]), int(raw[1])
                raw = input(f"  スイープ中心 x y (空Enter={w//2} {h//2}): ").strip().split()
                if raw:
                    sweep_cx, sweep_cy = int(raw[0]), int(raw[1])
                else:
                    sweep_cx, sweep_cy = w // 2, h // 2
                raw = input("  左右スイープ幅px (デフォルト300): ").strip()
                sweep_px = int(raw) if raw else 300
                raw = input("  スイープボタン L/R/M (デフォルトR): ").strip().upper()
                sweep_btn = raw if raw in ("L", "R", "M") else "R"
            except (ValueError, IndexError):
                print("  入力エラー\n")
                continue

            print(f"\n5秒後に開始します")
            countdown(5)

            print(f"  [1] クリック ({click_x}, {click_y})")
            mouse.click(click_x, click_y)
            time.sleep(0.3)

            print(f"  [2] スイープ中心へ移動 ({sweep_cx}, {sweep_cy})")
            mouse.move_cursor(sweep_cx, sweep_cy)
            time.sleep(0.2)

            left_x  = max(0, sweep_cx - sweep_px)
            right_x = min(w - 1, sweep_cx + sweep_px)

            print(f"  [3] {sweep_btn}ボタン押し下げ")
            mouse.press(sweep_btn)
            time.sleep(0.1)

            print(f"  [4] 左へ → ({left_x}, {sweep_cy})")
            move_and_report(mouse, left_x, sweep_cy, "左")

            print(f"  [5] 中央へ → ({sweep_cx}, {sweep_cy})")
            move_and_report(mouse, sweep_cx, sweep_cy, "中央")

            print(f"  [6] 右へ → ({right_x}, {sweep_cy})")
            move_and_report(mouse, right_x, sweep_cy, "右")

            print(f"  [7] 中央へ → ({sweep_cx}, {sweep_cy})")
            move_and_report(mouse, sweep_cx, sweep_cy, "中央")

            print(f"  [8] {sweep_btn}ボタン解放")
            mouse.release(sweep_btn)
            print()
            continue

        elif choice in ("1", "2", "3", "4", "5"):
            print("5秒後に開始します。カーソルをどけないでください")
            countdown(5)

        if choice == "1":
            move_and_report(mouse, w // 2, h // 2, "中央")

        elif choice == "2":
            margin = 50
            corners = [
                (margin,     margin,     "左上"),
                (w - margin, margin,     "右上"),
                (w - margin, h - margin, "右下"),
                (margin,     h - margin, "左下"),
                (w // 2,     h // 2,     "中央"),
            ]
            for tx, ty, label in corners:
                move_and_report(mouse, tx, ty, label)
                time.sleep(0.4)

        elif choice == "3":
            try:
                raw = input("  x y を入力: ").strip().split()
                tx, ty = int(raw[0]), int(raw[1])
                move_accurate_and_report(mouse, tx, ty)
            except (ValueError, IndexError):
                print("  入力エラー\n")

        elif choice == "4":
            cx, cy = mouse.get_cursor_pos()
            print(f"  クリック: ({cx}, {cy})")
            mouse.click(cx, cy)
            print("  送信完了\n")

        elif choice == "5":
            margin = 50
            corners = [
                (margin,     margin,     "左上"),
                (w - margin, margin,     "右上"),
                (w - margin, h - margin, "右下"),
                (margin,     h - margin, "左下"),
                (w // 2,     h // 2,     "中央"),
            ]
            for tx, ty, label in corners:
                move_accurate_and_report(mouse, tx, ty, label)
                time.sleep(0.4)

        elif choice != "q":
            print("  不明な入力\n")

    mouse.close()
    print("終了")


if __name__ == "__main__":
    main()
