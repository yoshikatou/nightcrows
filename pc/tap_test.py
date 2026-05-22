r"""Windows Touch Injection API のタップ動作確認用テスト。

実行すると 5 秒カウントダウン後、その時点のマウスカーソル位置に
仮想タッチを 1 回注入する。タッチハードウェアが無くても動く。

使い方:
  1. このスクリプトを実行
  2. Nightcrows の押したいボタン上にマウスカーソルを移動して置く
  3. 5 秒経過後、カーソル位置にタッチ注入される
  4. Nightcrows 側でボタンが反応したか確認

実行: ..\.venv\Scripts\python.exe tap_test.py
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

# ---------- 定数 (Win32 SDK より) ----------
PT_TOUCH = 0x00000002

POINTER_FLAG_INRANGE    = 0x00000004
POINTER_FLAG_INCONTACT  = 0x00000008
POINTER_FLAG_DOWN       = 0x00010000
POINTER_FLAG_UPDATE     = 0x00020000
POINTER_FLAG_UP         = 0x00040000

TOUCH_FEEDBACK_DEFAULT  = 0x00000001
TOUCH_FEEDBACK_INDIRECT = 0x00000002
TOUCH_FEEDBACK_NONE     = 0x00000003

TOUCH_MASK_CONTACTAREA  = 0x00000001
TOUCH_MASK_ORIENTATION  = 0x00000002
TOUCH_MASK_PRESSURE     = 0x00000004


# ---------- 構造体 ----------
class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType",             ctypes.c_uint32),
        ("pointerId",               ctypes.c_uint32),
        ("frameId",                 ctypes.c_uint32),
        ("pointerFlags",            ctypes.c_uint32),
        ("sourceDevice",            wintypes.HANDLE),
        ("hwndTarget",              wintypes.HWND),
        ("ptPixelLocation",         wintypes.POINT),
        ("ptHimetricLocation",      wintypes.POINT),
        ("ptPixelLocationRaw",      wintypes.POINT),
        ("ptHimetricLocationRaw",   wintypes.POINT),
        ("dwTime",                  wintypes.DWORD),
        ("historyCount",            ctypes.c_uint32),
        ("inputData",               ctypes.c_int32),
        ("dwKeyStates",             wintypes.DWORD),
        ("PerformanceCount",        ctypes.c_uint64),
        ("ButtonChangeType",        ctypes.c_int),
    ]


class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo",   POINTER_INFO),
        ("touchFlags",    ctypes.c_uint32),
        ("touchMask",     ctypes.c_uint32),
        ("rcContact",     wintypes.RECT),
        ("rcContactRaw",  wintypes.RECT),
        ("orientation",   ctypes.c_uint32),
        ("pressure",      ctypes.c_uint32),
    ]


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.InitializeTouchInjection.restype = wintypes.BOOL
user32.InitializeTouchInjection.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
user32.InjectTouchInput.restype = wintypes.BOOL
user32.InjectTouchInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(POINTER_TOUCH_INFO)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
kernel32.GetTickCount.restype = wintypes.DWORD
kernel32.GetTickCount.argtypes = []

SM_XVIRTUALSCREEN  = 76
SM_YVIRTUALSCREEN  = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def _enable_dpi_awareness() -> None:
    """プロセスを Per-Monitor V2 DPI Aware に設定する。

    DPI 非対応のままだと InjectTouchInput が仮想化座標を画面外と判断して
    ERROR_INVALID_PARAMETER (87) を返すため、起動時に必ず呼ぶ。
    """
    # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
    try:
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    # Win10 1607 以前のフォールバック
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def _make_contact(x: int, y: int, flags: int) -> POINTER_TOUCH_INFO:
    # ctypes.Structure() はゼロ初期化されるので dwTime / PerformanceCount /
    # historyCount は明示しない（Microsoft の C++ サンプルと同じ挙動）。
    ti = POINTER_TOUCH_INFO()
    ti.pointerInfo.pointerType = PT_TOUCH
    ti.pointerInfo.pointerId = 0
    ti.pointerInfo.ptPixelLocation.x = x
    ti.pointerInfo.ptPixelLocation.y = y
    ti.pointerInfo.pointerFlags = flags
    ti.touchFlags = 0
    ti.touchMask = (
        TOUCH_MASK_CONTACTAREA | TOUCH_MASK_ORIENTATION | TOUCH_MASK_PRESSURE
    )
    ti.rcContact.left   = x - 2
    ti.rcContact.top    = y - 2
    ti.rcContact.right  = x + 2
    ti.rcContact.bottom = y + 2
    ti.orientation = 90
    ti.pressure = 32000
    return ti


def tap(x: int, y: int, hold_ms: int = 50) -> None:
    """指定絶対座標を1回タップする（down → 待機 → up）。"""
    down = _make_contact(
        x, y,
        POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT,
    )
    if not user32.InjectTouchInput(1, ctypes.byref(down)):
        raise OSError(
            f"InjectTouchInput(down) failed: error={ctypes.get_last_error()}"
        )
    time.sleep(hold_ms / 1000)
    up = _make_contact(x, y, POINTER_FLAG_UP)
    if not user32.InjectTouchInput(1, ctypes.byref(up)):
        raise OSError(
            f"InjectTouchInput(up) failed: error={ctypes.get_last_error()}"
        )


def get_cursor_pos() -> tuple[int, int]:
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def main() -> None:
    _enable_dpi_awareness()

    # 構造体サイズ・画面範囲を表示（診断用）
    print("=== 環境情報 ===")
    print(f"  sizeof(POINTER_INFO)       = {ctypes.sizeof(POINTER_INFO)} (期待値: 96)")
    print(f"  sizeof(POINTER_TOUCH_INFO) = {ctypes.sizeof(POINTER_TOUCH_INFO)} (期待値: 144)")
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    print(f"  仮想スクリーン: ({vx},{vy}) - ({vx + vw - 1},{vy + vh - 1})  size={vw}x{vh}")
    print()

    # 機能フィードバックは NONE が最も無難（ハードウェア依存しない）
    if not user32.InitializeTouchInjection(1, TOUCH_FEEDBACK_NONE):
        raise OSError(
            f"InitializeTouchInjection failed: error={ctypes.get_last_error()}"
        )
    print("Touch Injection 初期化 OK (DPI aware, feedback=NONE)")
    print()
    print("=== タッチ注入テスト ===")
    print("Nightcrows の押したいボタン上にマウスカーソルを置いてください")
    print("（5秒後にその位置をタップ。Nightcrows をフォアグラウンドにしてください）")
    print()
    for i in range(5, 0, -1):
        print(f"  {i} ...")
        time.sleep(1)
    x, y = get_cursor_pos()
    print(f"\nタップ実行: ({x}, {y})")
    if not (vx <= x < vx + vw and vy <= y < vy + vh):
        print(f"  ⚠ 警告: 座標が仮想スクリーン外です ({vx},{vy})-({vx + vw},{vy + vh})")
    tap(x, y)
    print("送信完了。Nightcrows 側でボタンが反応したか確認してください。")


if __name__ == "__main__":
    main()
