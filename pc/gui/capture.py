"""ウィンドウキャプチャ。

優先順:
  1. PrintWindow(PW_RENDERFULLCONTENT)
     - フォアグラウンドを奪わずに描画を取得できる
     - 多くの通常アプリで動く
  2. 画面 DC からの BitBlt（ウィンドウの画面上の矩形を切り出し）
     - DirectX 系ゲームなど PrintWindow が失敗するケースのフォールバック
     - ウィンドウが画面に見えている必要がある（最前面・borderless fullscreen で OK）
     - 別ウィンドウで隠れている部分は取れない

戻り値は OpenCV 互換の BGR numpy 配列。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

import numpy as np

import win32con
import win32gui
import win32ui

_PW_RENDERFULLCONTENT = 0x00000002  # DWM レンダリング込み（Win 8.1+）


def get_client_size(hwnd: int) -> tuple[int, int]:
    rect = win32gui.GetClientRect(hwnd)
    return rect[2] - rect[0], rect[3] - rect[1]


def get_client_screen_rect(hwnd: int) -> tuple[int, int, int, int]:
    """クライアント領域の画面座標 (left, top, width, height) を返す。"""
    w, h = get_client_size(hwnd)
    left, top = win32gui.ClientToScreen(hwnd, (0, 0))
    return left, top, w, h


def _capture_via_printwindow(hwnd: int) -> np.ndarray | None:
    width, height = get_client_size(hwnd)
    if width <= 0 or height <= 0:
        return None

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None
    mfc_dc = save_dc = bitmap = None
    try:
        mfc_dc  = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap  = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)

        user32 = ctypes.windll.user32
        user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        user32.PrintWindow.restype  = wintypes.BOOL
        if not user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), _PW_RENDERFULLCONTENT):
            return None

        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        img = np.frombuffer(bits, dtype=np.uint8).reshape(
            info["bmHeight"], info["bmWidth"], 4
        )
        return img[:, :, :3].copy()  # BGRA → BGR
    except Exception:
        return None
    finally:
        if bitmap  is not None: win32gui.DeleteObject(bitmap.GetHandle())
        if save_dc is not None: save_dc.DeleteDC()
        if mfc_dc  is not None: mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def _capture_via_screen_bitblt(hwnd: int) -> np.ndarray | None:
    """画面 DC からウィンドウのクライアント領域を BitBlt で抜く。"""
    left, top, width, height = get_client_screen_rect(hwnd)
    if width <= 0 or height <= 0:
        return None

    screen_dc = win32gui.GetDC(0)
    if not screen_dc:
        return None
    mfc_dc = save_dc = bitmap = None
    try:
        mfc_dc  = win32ui.CreateDCFromHandle(screen_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap  = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        save_dc.BitBlt(
            (0, 0), (width, height), mfc_dc, (left, top), win32con.SRCCOPY
        )
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        img = np.frombuffer(bits, dtype=np.uint8).reshape(
            info["bmHeight"], info["bmWidth"], 4
        )
        return img[:, :, :3].copy()
    except Exception:
        return None
    finally:
        if bitmap  is not None: win32gui.DeleteObject(bitmap.GetHandle())
        if save_dc is not None: save_dc.DeleteDC()
        if mfc_dc  is not None: mfc_dc.DeleteDC()
        win32gui.ReleaseDC(0, screen_dc)


def capture_window(hwnd: int) -> np.ndarray | None:
    """指定ウィンドウのクライアント領域をキャプチャ。

    PrintWindow → 画面 BitBlt の順でフォールバック。
    """
    if not win32gui.IsWindow(hwnd):
        return None
    img = _capture_via_printwindow(hwnd)
    if img is not None and img.any():
        return img
    return _capture_via_screen_bitblt(hwnd)
