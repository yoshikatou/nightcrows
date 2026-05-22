r"""SendInput でマウスクリックを送る動作確認用テスト。

実行すると 5 秒カウントダウン後、その時点のマウスカーソル位置に
左クリックを 1 回送る。

使い方:
  1. このスクリプトを実行
  2. 反応を見たいボタン上にマウスカーソルを置く
  3. 5 秒経過後、カーソル位置にクリック注入される
  4. 対象アプリ側でクリックが反応したか確認

実行: ..\.venv\Scripts\python.exe click_test.py
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

INPUT_MOUSE          = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP   = 0x0004


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          wintypes.LONG),
        ("dy",          wintypes.LONG),
        ("mouseData",   wintypes.DWORD),
        ("dwFlags",     wintypes.DWORD),
        ("time",        wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("_u",   _INPUT_UNION),
    ]


user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SendInput.restype  = wintypes.UINT
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetCursorPos.restype  = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]


def _enable_dpi_awareness() -> None:
    """Per-Monitor V2 DPI Aware にしてマルチモニタでの座標ズレを防ぐ。"""
    try:
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def click(x: int, y: int, hold_ms: int = 30) -> None:
    """指定絶対座標にカーソルを移動して左クリック1回。"""
    user32.SetCursorPos(x, y)
    time.sleep(0.01)  # 移動が反映されるまで少し待つ
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dwFlags = MOUSEEVENTF_LEFTDOWN
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        raise OSError(f"SendInput(DOWN) failed: error={ctypes.get_last_error()}")
    time.sleep(hold_ms / 1000)
    inp.mi.dwFlags = MOUSEEVENTF_LEFTUP
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        raise OSError(f"SendInput(UP) failed: error={ctypes.get_last_error()}")


def get_cursor_pos() -> tuple[int, int]:
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def main() -> None:
    _enable_dpi_awareness()
    print("=== マウスクリック注入テスト ===")
    print("反応を見たいボタン上にマウスカーソルを置いてください")
    print("（5秒後にその位置を左クリック。対象ウィンドウをフォアグラウンドに）")
    print()
    for i in range(5, 0, -1):
        print(f"  {i} ...")
        time.sleep(1)
    x, y = get_cursor_pos()
    print(f"\nクリック実行: ({x}, {y})")
    click(x, y)
    print("送信完了。対象側でクリックが反応したか確認してください。")


if __name__ == "__main__":
    main()
