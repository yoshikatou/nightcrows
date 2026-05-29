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

# 短距離移動の精度優先パラメータ。
# 目標までの距離がこの px 以下なら click_at が精度優先パラメータに切り替える。
# 短距離ではポインター加速・HID 端数の影響が大きく出るため、
# ステップを細かく・間隔を広く取って 1 ステップあたりの加速影響を抑える。
_SHORT_MOVE_THRESHOLD_PX = 80
_SHORT_MOVE_STEP = 5         # max_step (px/event)
_SHORT_MOVE_DELAY = 0.04     # event 間 sleep (秒)

# 短距離 detour（遠回り）モード。短距離移動が苦手な環境向けに、
# 一度 target から離れた位置へジャンプしてから長距離アプローチで戻す。
# 短距離での HID 加速立ち上がり不足や、終端の沈み込みを回避する狙い。
_SHORT_MOVE_DETOUR_OFFSET_PX = 300


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
        self._speed_scale: float = 1.0  # HID 1単位あたりの実移動画素数（calibrate() で測定）
        # 短距離 click_at で「detour 経由」アプローチを使うか（短距離 HID 精度問題対策）
        self.short_move_detour: bool = True
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
        """指定座標にカーソルを移動してクリックを送信する（SetCursorPos のジャンプ版）。

        高速だが、ゲームのチート対策で SetCursorPos がブロックされる環境では
        カーソルが動かず、最後にいた位置でクリックされてしまうことがある。
        その場合は `click_at` を使うか、tap 側で自動リトライされる。
        """
        self.move_cursor(x, y)
        resp = self._cmd(f"CLICK {button.upper()} {hold_ms}")
        if resp != "OK":
            raise RuntimeError(f"Pico CLICK エラー: {resp}")

    def click_at(
        self, x: int, y: int, button: str = "L", hold_ms: int = 30,
    ) -> tuple[int, int]:
        """HID 相対移動で目標座標へ動かしてからクリック（SetCursorPos を使わない確実版）。

        Nightcrows 等のチート対策で SetCursorPos がブロックされる状況でも、
        実マウス相当の HID 相対移動なら通る。戻り値は最終カーソル位置。

        短距離移動はポインター加速の影響と HID 端数の影響を受けやすいため、
        `short_move_detour=True`（既定）なら target から離れた位置を経由させて
        必ず「長距離アプローチ」になるようにする。短距離直行モードに戻したい
        場合はインスタンスで `mouse.short_move_detour = False` を立てる。
        """
        cx, cy = self.get_cursor_pos()
        dist = max(abs(x - cx), abs(y - cy))
        if dist <= _SHORT_MOVE_THRESHOLD_PX and self.short_move_detour:
            # detour: target から大きく離れた位置に一旦移動 → 長距離アプローチで target へ
            # 画面端へ出ないよう、target 座標が大きい側からは引き、小さい側からは加える
            off = _SHORT_MOVE_DETOUR_OFFSET_PX
            detour_x = x - off if x > off else x + off
            detour_y = y - off if y > off else y + off
            self.move_to_accurate(detour_x, detour_y)
            fx, fy = self.move_to_accurate(x, y)
        elif dist <= _SHORT_MOVE_THRESHOLD_PX:
            # 旧挙動: 短距離直行（detour 無効時のフォールバック）
            fx, fy = self.move_to_accurate(
                x, y, step=_SHORT_MOVE_STEP, delay=_SHORT_MOVE_DELAY,
            )
        else:
            fx, fy = self.move_to_accurate(x, y)
        resp = self._cmd(f"CLICK {button.upper()} {hold_ms}")
        if resp != "OK":
            raise RuntimeError(f"Pico CLICK エラー: {resp}")
        return fx, fy

    def move(self, dx: int, dy: int) -> None:
        """相対移動を Pico HID で送信する (-127〜127)。"""
        resp = self._cmd(f"MOVE {dx} {dy}")
        if resp != "OK":
            raise RuntimeError(f"Pico MOVE エラー: {resp}")

    def calibrate(self, test_hid: int = 60) -> float:
        """ポインター速度スケールを測定する（HID 1単位 = 何画素か）。

        小さな HID 移動を送り、実際の画素移動量から倍率を算出して保存する。
        move_to() はこの値を使って HID 単位を自動補正する。
        """
        bx, by = self.get_cursor_pos()
        self.move(test_hid, 0)
        time.sleep(0.1)
        ax, ay = self.get_cursor_pos()
        actual = ax - bx
        self.move_cursor(bx, by)  # SetCursorPos で元位置に戻す
        time.sleep(0.05)

        if actual == 0:
            self._speed_scale = 1.0
        else:
            self._speed_scale = actual / test_hid
        return self._speed_scale

    def press(self, button: str = "L") -> None:
        """マウスボタンを押したまま（離さない）。"""
        resp = self._cmd(f"HOLD {button.upper()}")
        if resp != "OK":
            raise RuntimeError(f"Pico HOLD エラー: {resp}")

    def release(self, button: str = "") -> None:
        """マウスボタンを離す。button省略で全ボタン解放。"""
        resp = self._cmd(f"RELEASE {button.upper()}")
        if resp != "OK":
            raise RuntimeError(f"Pico RELEASE エラー: {resp}")

    def move_to(
        self,
        x: int,
        y: int,
        max_step: int = 20,
        min_step: int = 1,
        delay: float = 0.02,
    ) -> None:
        """Pico HID の相対移動でカーソルを目標座標まで動かす。

        残距離が縮むにつれてステップを自動的に小さくし、
        目標付近では低速・高精度になる（イーズアウト）。

        max_step: 遠距離時の最大移動量 (px/event)
        min_step: 近距離時の最小移動量 (px/event)
        delay:    イベント間の待機秒数
        """
        cx, cy = self.get_cursor_pos()
        dx, dy = x - cx, y - cy
        while dx != 0 or dy != 0:
            dist = max(abs(dx), abs(dy))
            # 残距離に比例してステップを縮小（イーズアウト）
            pix_step = max(min_step, min(max_step, dist // 3))
            px = max(-pix_step, min(pix_step, dx))
            py = max(-pix_step, min(pix_step, dy))
            # ポインター速度スケールで HID 単位に変換
            hx = int(px / self._speed_scale)
            hy = int(py / self._speed_scale)
            # 残距離があるのに HID 単位で 0 に丸まると永久に収束しないため
            # 最小 1 単位（= speed_scale 画素相当）に丸める。
            # これがないと speed_scale > 1.0 の環境で move_to_accurate の
            # 補正ループが残差を埋められずタイムアウトする。
            if hx == 0 and px != 0:
                hx = 1 if px > 0 else -1
            if hy == 0 and py != 0:
                hy = 1 if py > 0 else -1
            if hx == 0 and hy == 0:
                break
            self.move(hx, hy)
            dx -= px
            dy -= py
            if delay > 0:
                time.sleep(delay)

    def move_to_accurate(
        self,
        x: int,
        y: int,
        tolerance: int = 3,
        max_iter: int = 8,
        step: int = 20,
        delay: float = 0.02,
        on_iter: "callable | None" = None,
    ) -> tuple[int, int]:
        """誤差を自動補正しながら目標座標へ移動する。

        GetCursorPos で実際の着地点を計測し、残差があれば補正移動を繰り返す。
        ポインター加速が有効でも収束する（ただし iterations が増える）。

        on_iter: (iteration, actual_x, actual_y, error_x, error_y) を受け取るコールバック。
        戻り値: 最終的な (actual_x, actual_y)。
        """
        self.move_to(x, y, max_step=step, delay=delay)

        for i in range(max_iter):
            time.sleep(0.05)
            cx, cy = self.get_cursor_pos()
            ex, ey = x - cx, y - cy
            if on_iter:
                on_iter(i, cx, cy, ex, ey)
            if abs(ex) <= tolerance and abs(ey) <= tolerance:
                break
            # 補正: 残差を小ステップで送る（大きな加速を避けるため max_step 小さく）
            corr_step = max(1, min(10, max(abs(ex), abs(ey)) // 2))
            self.move_to(cx + ex, cy + ey, max_step=corr_step, delay=delay)

        time.sleep(0.05)
        return self.get_cursor_pos()

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
