"""
ランダムタップテスト

ブラウザのタップ確認サイトなどでタッチ注入の動作を視覚的に確認する。
画面上にランダムな座標をタップし続ける。

使用方法:
    python tests/test_random_tap.py
    python tests/test_random_tap.py --count 20 --interval 1.5
"""
from __future__ import annotations

import argparse
import random
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
log = logging.getLogger("random_tap")

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


def build_touch(action: int, x: int, y: int,
                screen_w: int, screen_h: int,
                pressure: int = 0xFFFF) -> bytes:
    return struct.pack(
        ">BBqIIHHHII",
        2, action, 0,
        x, y,
        screen_w, screen_h,
        pressure, 0, 0,
    )


def do_tap(ctrl: socket.socket, x: int, y: int,
           screen_w: int, screen_h: int, hold_ms: int = 120):
    ctrl.sendall(build_touch(ACTION_DOWN, x, y, screen_w, screen_h, 0xFFFF))
    time.sleep(hold_ms / 1000.0)
    ctrl.sendall(build_touch(ACTION_UP, x, y, screen_w, screen_h, 0))
    log.info("TAP (%4d, %4d)  screen=%dx%d", x, y, screen_w, screen_h)


def get_display_size() -> tuple[int, int]:
    try:
        r = subprocess.run(
            [ADB, "-s", SERIAL, "shell", "wm", "size"],
            capture_output=True, text=True, timeout=5,
        )
        w, h = 1080, 1920
        for line in r.stdout.splitlines():
            if "Physical size" in line or "Override size" in line:
                w, h = map(int, line.split(":")[-1].strip().split("x"))
                break

        r2 = subprocess.run(
            [ADB, "-s", SERIAL, "shell",
             "dumpsys input | grep -i orientation"],
            capture_output=True, text=True, timeout=5,
        )
        rotated = any(
            tok in ("1", "3")
            for line in r2.stdout.splitlines()
            for tok in line.split()
            if "rientation" in line
        )
        if rotated and w < h:
            w, h = h, w

        log.info("display: %dx%d (rotated=%s)", w, h, rotated)
        return w, h
    except Exception as e:
        log.warning("get_display_size failed: %s  → fallback 1080x1920", e)
        return 1080, 1920


def kill_existing_servers():
    subprocess.run([ADB, "-s", SERIAL, "shell", "pkill", "-f", f"scid={SCID}"],
                   capture_output=True, timeout=5)
    subprocess.run([ADB, "-s", SERIAL, "forward", "--remove", f"tcp:{TCP_PORT}"],
                   capture_output=True, timeout=5)
    time.sleep(1.5)


def push_server():
    r = subprocess.run(
        [ADB, "-s", SERIAL, "shell", "ls", SERVER_REMOTE],
        capture_output=True, timeout=5,
    )
    if r.returncode == 0 and SERVER_REMOTE.encode() in r.stdout:
        return
    subprocess.run(
        [ADB, "-s", SERIAL, "push", SERVER_LOCAL, SERVER_REMOTE],
        check=True, timeout=15,
    )


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
    def try_connect():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect(("127.0.0.1", TCP_PORT))
        return s

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

    def drain(s):
        try:
            s.settimeout(1.0)
            while True:
                if not s.recv(65536):
                    break
        except Exception:
            pass
    threading.Thread(target=drain, args=(video_sock,), daemon=True).start()

    time.sleep(0.1)
    for i in range(10):
        try:
            ctrl = try_connect()
            ctrl.settimeout(None)
            log.info("control socket connected")
            return video_sock, ctrl
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("control socket connection failed")


def cleanup(proc: subprocess.Popen):
    try:
        subprocess.run([ADB, "-s", SERIAL, "forward", "--remove", f"tcp:{TCP_PORT}"],
                       timeout=3, capture_output=True)
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=30,
                        help="タップ回数 (default: 30)")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="タップ間隔秒 (default: 1.0)")
    parser.add_argument("--hold", type=int, default=120,
                        help="タップ保持ミリ秒 (default: 120)")
    parser.add_argument("--margin", type=int, default=50,
                        help="画面端マージンpx (default: 50)")
    args = parser.parse_args()

    log.info("=== ランダムタップテスト count=%d interval=%.1fs ===",
             args.count, args.interval)

    server_proc = video_sock = ctrl_sock = None
    try:
        kill_existing_servers()
        push_server()

        sw, sh = get_display_size()
        m = args.margin

        subprocess.run(
            [ADB, "-s", SERIAL, "forward",
             f"tcp:{TCP_PORT}", f"localabstract:scrcpy_{SCID:08x}"],
            check=True, timeout=5,
        )
        server_proc = start_server()
        video_sock, ctrl_sock = connect_sockets()

        log.info("タップ開始: 範囲 (%d-%d, %d-%d)", m, sw - m, m, sh - m)
        for i in range(args.count):
            x = random.randint(m, sw - m)
            y = random.randint(m, sh - m)
            do_tap(ctrl_sock, x, y, sw, sh, hold_ms=args.hold)
            time.sleep(args.interval)

        log.info("完了: %d タップ", args.count)

    except KeyboardInterrupt:
        log.info("中断")
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
