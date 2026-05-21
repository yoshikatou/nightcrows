"""
キャプチャ動作テスト
scrcpy H.264ストリーミングでフレームを取得し、PNGとして保存する。

使用方法:
    python tests/test_capture.py
    python tests/test_capture.py --serial 192.168.255.57:34497
    python tests/test_capture.py --mode screencap   # screencapのみでテスト
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

# SmartOps参照コードをパスに追加
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "refs" / "SmartOps" / "src"))

from core.device_info import DeviceInfo
from core.scrcpy_stream import ScrcpyStream, StreamStatus
from core.stream_config import StreamConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("test_capture")

DEFAULT_SERIAL = "192.168.255.57:34497"
OUTPUT_DIR = ROOT / "debug"


def test_scrcpy(serial: str, timeout: int = 30) -> bool:
    """scrcpy H.264ストリーミングでフレームを取得してPNG保存"""
    log.info("=== scrcpy H.264 キャプチャテスト ===")
    log.info("デバイス: %s", serial)

    # セマフォを1台用に初期化
    ScrcpyStream._conn_semaphore = None       # 制限なし
    ScrcpyStream._socket_semaphore = None
    ScrcpyStream._stream_slot_sem = threading.Semaphore(1)
    ScrcpyStream._screencap_sem = None

    # 横向きゲーム: wm sizeは物理縦サイズ(1220x2712)を返すため
    # _fetch_screen_sizeをオーバーライドして横向き寸法(2712x1220)を使用する
    config = StreamConfig(
        scrcpy_dir=r"C:\scrcpy",
        thumb_w=2712,   # 横向き幅
        max_fps=15,
        video_bitrate=2_000_000,
    )

    device = DeviceInfo(serial=serial, model_name="XIG04", group="test", index=1)
    stream = ScrcpyStream(device, port=config.base_port, config=config)

    # 横向き寸法を強制設定（wm sizeの縦サイズを上書き）
    _orig_fetch = stream._fetch_screen_size
    def _fetch_landscape():
        _orig_fetch()
        if stream.real_w < stream.real_h:  # 縦サイズが返った場合はスワップ
            stream.real_w, stream.real_h = stream.real_h, stream.real_w
    stream._fetch_screen_size = _fetch_landscape

    stream.start()
    log.info("ストリーム開始。フレームを待機中... (最大%d秒)", timeout)

    deadline = time.time() + timeout
    frame = None

    while time.time() < deadline:
        frame = stream.peek_frame()
        if frame is not None:
            break
        status = stream.status
        if status in (StreamStatus.ERROR, StreamStatus.DISCONNECTED):
            log.error("ストリームエラー: %s", status)
            break
        time.sleep(0.2)

    stream.stop()

    if frame is None:
        log.error("フレーム取得失敗")
        return False

    out_path = OUTPUT_DIR / "capture_scrcpy.png"
    frame.save(str(out_path))
    log.info("保存成功: %s (%dx%d)", out_path, frame.width, frame.height)
    return True


def test_screencap(serial: str) -> bool:
    """adb screencapでフレームを取得してPNG保存"""
    import io
    import subprocess
    from PIL import Image

    log.info("=== screencap キャプチャテスト ===")
    log.info("デバイス: %s", serial)

    result = subprocess.run(
        ["adb", "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True,
        timeout=15,
    )

    if result.returncode != 0 or not result.stdout:
        log.error("screencap失敗: %s", result.stderr.decode(errors="replace"))
        return False

    img = Image.open(io.BytesIO(result.stdout))
    out_path = OUTPUT_DIR / "capture_screencap.png"
    img.save(str(out_path))
    log.info("保存成功: %s (%dx%d)", out_path, img.width, img.height)
    return True


def main():
    parser = argparse.ArgumentParser(description="nightcrows キャプチャテスト")
    parser.add_argument("--serial", default=DEFAULT_SERIAL, help="デバイスシリアル")
    parser.add_argument(
        "--mode",
        choices=["scrcpy", "screencap", "both"],
        default="both",
        help="テストモード (default: both)",
    )
    parser.add_argument("--timeout", type=int, default=30, help="scrcpy待機タイムアウト秒")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    results = {}

    if args.mode in ("screencap", "both"):
        results["screencap"] = test_screencap(args.serial)

    if args.mode in ("scrcpy", "both"):
        results["scrcpy"] = test_scrcpy(args.serial, timeout=args.timeout)

    print("\n--- 結果 ---")
    for name, ok in results.items():
        print(f"  {name}: {'OK' if ok else 'FAIL'}")

    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
