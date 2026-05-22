"""
Raspberry Pi Pico HID マウスコントローラ (PC 側)

Pico (CircuitPython + adafruit_hid) をシリアルポート経由で制御し、
物理マウス HID 入力としてクリックを送信する。
SendInput と異なり LLMHF_INJECTED フラグが立たないため、
Raw Input でフィルタするゲームにも通る。

使い方:
    from pico_mouse import PicoMouse
    with PicoMouse() as m:
        m.click(960, 540)        # 左クリック
        m.click(960, 540, "R")   # 右クリック

依存: pyserial  (pip install pyserial)
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

try:
    import serial
    import serial.tools.list_ports
except ImportError as e:
    raise ImportError("pyserial が必要です: pip install pyserial") from e

# CircuitPython (Adafruit) の USB Vendor ID
# 0x239A = Adafruit (CircuitPython), 0x2E8A = Raspberry Pi (MicroPython)
_PICO_VIDS = {0x239A, 0x2E8A}
_BAUD = 115200
_TIMEOUT = 2.0


def _enable_dpi_awareness() -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
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


def find_pico_port() -> str | None:
    """USB に接続された Pico の COM ポートを自動検出する。"""
    ports = list_pico_ports()
    # CDC は2ポート出る: 小さい方=data、大きい方=console(sys.stdin/stdout)
    return ports[-1][0] if ports else None


def list_pico_ports() -> list[tuple[str, str]]:
    """接続中の Pico デバイス一覧を返す (port, description)。"""
    return [
        (p.device, p.description)
        for p in serial.tools.list_ports.comports()
        if p.vid in _PICO_VIDS
    ]


class PicoMouse:
    """
    Pico HID マウスコントローラ。

    カーソル移動は Windows API (SetCursorPos) で即座に行い、
    クリックのみ Pico HID 経由で物理入力として送信する。
    """

    def __init__(self, port: str | None = None) -> None:
        _enable_dpi_awareness()

        resolved = port or find_pico_port()
        if resolved is None:
            candidates = list_pico_ports()
            hint = f"検出なし。手動で port= を指定してください" if not candidates else str(candidates)
            raise RuntimeError(f"Pico が見つかりません: {hint}")

        # CircuitPython はシリアルオープン時に再起動する場合があるので長めに待つ
        self._ser = serial.Serial(resolved, _BAUD, timeout=_TIMEOUT)
        time.sleep(2.0)
        self._ser.reset_input_buffer()

        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SetCursorPos.restype = wintypes.BOOL
        self._user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self._user32.GetCursorPos.restype = wintypes.BOOL
        self._user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]

        self.port = resolved
        self.ping()  # 疎通確認

    def ping(self) -> None:
        resp = self._cmd("PING")
        if resp != "OK":
            raise RuntimeError(f"Pico から応答なし: {resp!r}")

    def get_cursor_pos(self) -> tuple[int, int]:
        p = wintypes.POINT()
        self._user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    def move_cursor(self, x: int, y: int) -> None:
        """カーソルを絶対座標に移動 (SetCursorPos)。"""
        self._user32.SetCursorPos(x, y)
        time.sleep(0.01)

    def click(
        self, x: int, y: int, button: str = "L", hold_ms: int = 30
    ) -> None:
        """指定座標にカーソルを移動してクリックを送信する。"""
        self.move_cursor(x, y)
        resp = self._cmd(f"CLICK {button.upper()} {hold_ms}")
        if resp != "OK":
            raise RuntimeError(f"Pico CLICK エラー: {resp}")

    def move(self, dx: int, dy: int) -> None:
        """相対移動を Pico HID で送信する (-127〜127)。"""
        resp = self._cmd(f"MOVE {dx} {dy}")
        if resp != "OK":
            raise RuntimeError(f"Pico MOVE エラー: {resp}")

    def _cmd(self, cmd: str) -> str:
        self._ser.write((cmd + "\n").encode())
        line = self._ser.readline()
        return line.decode(errors="replace").strip()

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()

    def __enter__(self) -> "PicoMouse":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
