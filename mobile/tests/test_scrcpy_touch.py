"""
scrcpy controlソケット経由のタッチ注入テスト

使用方法:
    python tests/test_scrcpy_touch.py --target settings  # 設定アプリ（動作確認用）
    python tests/test_scrcpy_touch.py --target game      # ゲームのハンバーガーメニュー
    python tests/test_scrcpy_touch.py --target home      # ホーム画面
"""
from __future__ import annotations

import argparse
import socket
import struct
import subprocess
import sys
import time
import threading
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("scrcpy_touch")

SERIAL        = "192.168.255.57:34497"
ADB           = r"C:\scrcpy\adb.exe"
SERVER_LOCAL  = r"C:\scrcpy\scrcpy-server"
SERVER_REMOTE = "/data/local/tmp/scrcpy-server.jar"
SERVER_VER    = "3.3.4"
TCP_PORT      = 27201
SCID          = 2

ACTION_DOWN = 0
ACTION_UP   = 1
ACTION_MOVE = 2


# ---------------------------------------------------------------------------
# scrcpy touch protocol
# ---------------------------------------------------------------------------

def build_touch(action: int, x: int, y: int,
                screen_w: int, screen_h: int,
                pressure: int = 0xFFFF) -> bytes:
    """INJECT_TOUCH_EVENT: 32 bytes"""
    return struct.pack(
        ">BBqIIHHHII",
        2,          # type: INJECT_TOUCH_EVENT
        action,
        0,          # pointerId
        x, y,
        screen_w, screen_h,
        pressure,
        0,          # actionButton
        0,          # buttons
    )


def do_tap(ctrl: socket.socket, x: int, y: int,
           screen_w: int, screen_h: int, hold_ms: int = 3000):
    log.info("TAP (%d, %d) hold=%dms", x, y, hold_ms)
    subprocess.run(
        [ADB, "-s", SERIAL, "shell", "input", "swipe",
         str(x), str(y), str(x), str(y), str(hold_ms)],
        capture_output=True, timeout=hold_ms // 1000 + 5,
    )


def do_swipe(ctrl: socket.socket,
             x1: int, y1: int, x2: int, y2: int,
             screen_w: int, screen_h: int,
             steps: int = 50, duration_ms: int = 3000):
    log.info("SWIPE (%d,%d)->(%d,%d) %dms", x1, y1, x2, y2, duration_ms)
    subprocess.run(
        [ADB, "-s", SERIAL, "shell",
         "input", "swipe",
         str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
        capture_output=True, timeout=duration_ms // 1000 + 5,
    )
    log.info("SWIPE done")


# ---------------------------------------------------------------------------
# Device helpers
# ---------------------------------------------------------------------------

def get_display_size() -> tuple[int, int]:
    """wm size + rotation から実際のストリーム寸法(w, h)を返す。
    landscape時は w>h になるよう調整。
    """
    try:
        # 物理サイズ取得
        r = subprocess.run(
            [ADB, "-s", SERIAL, "shell", "wm", "size"],
            capture_output=True, text=True, timeout=5,
        )
        w, h = 1080, 1920
        for line in r.stdout.splitlines():
            if "Physical size" in line or "Override size" in line:
                part = line.split(":")[-1].strip()
                w, h = map(int, part.split("x"))
                break

        # 回転取得
        r2 = subprocess.run(
            [ADB, "-s", SERIAL, "shell",
             "dumpsys", "input", "|", "grep", "-i", "orientation"],
            capture_output=True, text=True, timeout=5,
        )
        rotated = False
        for line in r2.stdout.splitlines():
            if "Orientation" in line or "orientation" in line:
                for tok in line.split():
                    if tok in ("1", "3"):  # ROTATION_90 or ROTATION_270
                        rotated = True
                        break

        if rotated and w < h:
            w, h = h, w   # 横向き: wを大きく

        log.info("display size: %dx%d (rotated=%s)", w, h, rotated)
        return w, h
    except Exception as e:
        log.warning("get_display_size failed: %s", e)
        return 1080, 1920


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------

def kill_existing_servers():
    log.info("既存のscrcpyサーバーを終了...")
    # scid が一致するインスタンスだけを終了（他セッションを巻き込まない）
    subprocess.run(
        [ADB, "-s", SERIAL, "shell", "pkill", "-f", f"scid={SCID}"],
        capture_output=True, timeout=5,
    )
    subprocess.run(
        [ADB, "-s", SERIAL, "forward", "--remove", f"tcp:{TCP_PORT}"],
        capture_output=True, timeout=5,
    )
    time.sleep(1.5)


def push_server():
    r = subprocess.run(
        [ADB, "-s", SERIAL, "shell", "ls", SERVER_REMOTE],
        capture_output=True, timeout=5,
    )
    if r.returncode == 0 and SERVER_REMOTE.encode() in r.stdout:
        log.info("server already on device")
        return
    log.info("pushing scrcpy-server...")
    subprocess.run(
        [ADB, "-s", SERIAL, "push", SERVER_LOCAL, SERVER_REMOTE],
        check=True, timeout=15,
    )


def setup_forward():
    subprocess.run(
        [ADB, "-s", SERIAL, "forward",
         f"tcp:{TCP_PORT}", f"localabstract:scrcpy_{SCID:08x}"],
        check=True, timeout=5,
    )
    log.info("forward tcp:%d -> scrcpy_%08x", TCP_PORT, SCID)


def start_server() -> subprocess.Popen:
    cmd = [
        ADB, "-s", SERIAL, "shell",
        f"CLASSPATH={SERVER_REMOTE}",
        "app_process", "/",
        "com.genymobile.scrcpy.Server",
        SERVER_VER,
        f"scid={SCID}",
        "tunnel_forward=true",
        "audio=false",
        "control=true",
        "video=true",
        "cleanup=false",
        "send_frame_meta=false",
        "send_device_meta=false",
        "video_codec=h264",
        "max_size=2712",
        "video_bit_rate=2000000",
        "max_fps=5",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    log.info("scrcpy-server PID=%d", proc.pid)

    def drain_stderr(p):
        for line in p.stderr:
            txt = line.decode(errors="replace").rstrip()
            if txt:
                log.debug("SRV> %s", txt)
    threading.Thread(target=drain_stderr, args=(proc,), daemon=True).start()
    time.sleep(4.5)
    return proc


def connect_sockets() -> tuple[socket.socket, socket.socket]:
    def try_connect() -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect(("127.0.0.1", TCP_PORT))
        return s

    # 1st connection = video socket
    video_sock = None
    for i in range(20):
        try:
            video_sock = try_connect()
            log.info("video socket connected (attempt %d)", i + 1)
            break
        except OSError:
            time.sleep(0.3)
    else:
        raise RuntimeError("video socket connection failed")

    # バックグラウンドでドレイン（バッファブロック防止）
    def drain(s: socket.socket):
        try:
            s.settimeout(1.0)
            while True:
                if not s.recv(65536):
                    break
        except Exception:
            pass
    threading.Thread(target=drain, args=(video_sock,), daemon=True).start()

    # 2nd connection = control socket
    time.sleep(0.1)
    for i in range(10):
        try:
            ctrl_sock = try_connect()
            ctrl_sock.settimeout(None)   # タイムアウトなし（ブロックしない送信用）
            log.info("control socket connected")
            return video_sock, ctrl_sock
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("control socket connection failed")


def cleanup(proc: subprocess.Popen):
    try:
        subprocess.run(
            [ADB, "-s", SERIAL, "forward", "--remove", f"tcp:{TCP_PORT}"],
            timeout=3, capture_output=True,
        )
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["game", "settings", "home", "current", "swipe_lr", "tap5"],
                        default="settings")
    args = parser.parse_args()

    log.info("=== scrcpy touch test (target=%s) ===", args.target)

    server_proc = video_sock = ctrl_sock = None
    try:
        kill_existing_servers()
        push_server()

        if args.target == "settings":
            log.info("設定アプリを起動...")
            subprocess.run(
                [ADB, "-s", SERIAL, "shell",
                 "am", "start", "-a", "android.settings.SETTINGS"],
                capture_output=True, timeout=10,
            )
            time.sleep(2.0)
        elif args.target == "home":
            subprocess.run(
                [ADB, "-s", SERIAL, "shell", "input", "keyevent", "3"],
                capture_output=True, timeout=5,
            )
            time.sleep(1.0)
        elif args.target == "current":
            pass  # 現在の画面をそのまま使う

        # 現在の画面サイズを取得
        sw, sh = get_display_size()
        log.info("使用するscreen_w=%d screen_h=%d", sw, sh)

        setup_forward()
        server_proc = start_server()
        video_sock, ctrl_sock = connect_sockets()

        if args.target == "settings":
            # 設定アプリは縦向きが多い。縦向きなら sw<sh のはず。
            # 中央付近をタップ
            cx, cy = sw // 2, sh // 2
            log.info("設定: 中央 (%d,%d) をタップ", cx, cy)
            do_tap(ctrl_sock, cx, cy, sw, sh)
            time.sleep(1.5)
            # 上から1/4あたり（最初の項目）をタップ
            tx, ty = sw // 2, sh // 4
            log.info("設定: 上部 (%d,%d) をタップ", tx, ty)
            do_tap(ctrl_sock, tx, ty, sw, sh)
            time.sleep(1.5)

        elif args.target in ("home", "current"):
            cx, cy = sw // 2, sh // 2
            log.info("中央 (%d,%d) をタップ", cx, cy)
            do_tap(ctrl_sock, cx, cy, sw, sh)
            time.sleep(1.5)

        elif args.target == "swipe_lr":
            cx, cy = sw // 2, sh // 2
            log.info("左→右スワイプ (%d,%d)->(%d,%d)", cx - 300, cy, cx + 300, cy)
            do_swipe(ctrl_sock, cx - 300, cy, cx + 300, cy, sw, sh, steps=50, duration_ms=3000)

        elif args.target == "tap5":
            input("準備ができたら Enter を押してください...")
            log.info("スタート!")
            points = [
                (sw // 4,     sh // 4),      # 左上
                (sw * 3 // 4, sh // 4),      # 右上
                (sw // 2,     sh // 2),      # 中央
                (sw // 4,     sh * 3 // 4),  # 左下
                (sw * 3 // 4, sh * 3 // 4),  # 右下
            ]
            for i, (x, y) in enumerate(points):
                log.info("タップ %d/5: (%d, %d)", i + 1, x, y)
                do_tap(ctrl_sock, x, y, sw, sh, hold_ms=3000)
                if i < 4:
                    log.info("5秒待機...")
                    time.sleep(5.0)

        elif args.target == "game":
            # 横向きゲーム。sw=2712, sh=1220。
            # 右上ハンバーガーメニュー（スクリーンショット確認済み座標）
            log.info("ゲーム: 右上メニュー (2582,60) をタップ")
            do_tap(ctrl_sock, 2582, 60, sw, sh)
            time.sleep(1.5)

        log.info("完了")

    except Exception as e:
        log.error("エラー: %s", e)
        import traceback; traceback.print_exc()
        sys.exit(1)
    finally:
        if ctrl_sock:  ctrl_sock.close()
        if video_sock: video_sock.close()
        if server_proc: cleanup(server_proc)


if __name__ == "__main__":
    main()
