"""ADB / scrcpy 操作のラッパ。"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from typing import Callable

ADB = r"C:\scrcpy\adb.exe"
SCRCPY = r"C:\scrcpy\scrcpy.exe"

DEFAULT_SERIAL = os.environ.get("ADB_SERIAL", "192.168.255.57:34497")


def screencap(serial: str, timeout: float = 15.0) -> bytes:
    """現在の画面の PNG バイト列を返す。

    Windows の adb.exe は exec-out のバイナリ出力で \\r\\n 変換を行うことがある。
    PNG シグネチャ検出で破損を判定し、pull 方式にフォールバックする。
    """
    import tempfile, os as _os

    r = subprocess.run(
        [ADB, "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"screencap failed (exec-out): {r.stderr.decode(errors='replace')}")

    data = r.stdout
    # PNG 正規シグネチャは 89 50 4E 47 0D 0A 1A 0A （\r\n を含む）
    # → 先頭8バイトが一致すれば汚染なし、そのまま返す
    _PNG_SIG = b"\x89PNG\r\n\x1a\n"
    if data[:8] == _PNG_SIG:
        return data

    # シグネチャが崩れている場合のみ CRLF 修復を試みる
    # Windows adb.exe が \n → \r\n 変換した場合: 0D 0D 0A → 0D 0A に戻す
    if data[:4] == b"\x89PNG":
        fixed = data.replace(b"\r\n", b"\n")
        if fixed[:8] == _PNG_SIG:
            return fixed

    # フォールバック: デバイス上に保存して pull
    remote = "/sdcard/__nightcrows_sc__.png"
    subprocess.run([ADB, "-s", serial, "shell", "screencap", "-p", remote],
                   capture_output=True, timeout=timeout)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        local = f.name
    try:
        r2 = subprocess.run([ADB, "-s", serial, "pull", remote, local],
                            capture_output=True, timeout=timeout)
        if r2.returncode != 0:
            raise RuntimeError(f"pull failed: {r2.stderr.decode(errors='replace')}")
        with open(local, "rb") as f:
            return f.read()
    finally:
        subprocess.run([ADB, "-s", serial, "shell", "rm", remote],
                       capture_output=True, timeout=5)
        _os.unlink(local)


def input_swipe(serial: str, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
    duration_ms = max(1, int(duration_ms))
    subprocess.run(
        [ADB, "-s", serial, "shell", "input", "swipe",
         str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(duration_ms)],
        capture_output=True, timeout=duration_ms // 1000 + 5,
    )


def input_keyevent(serial: str, keycode: str) -> None:
    """Android キーイベントを送信する。keycode は文字列 ('KEYCODE_BACK' or '4' など)。"""
    subprocess.run(
        [ADB, "-s", serial, "shell", "input", "keyevent", keycode],
        capture_output=True, timeout=5,
    )


def adb_connect(serial: str, timeout: float = 10.0) -> tuple[bool, str]:
    """`adb connect` を実行し (成功判定, 標準出力+エラー) を返す。"""
    try:
        r = subprocess.run(
            [ADB, "connect", serial],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    low = out.lower()
    success = ("connected to" in low or "already connected" in low) \
        and "cannot" not in low and "failed" not in low
    return success, out


def adb_ping(serial: str, timeout: float = 3.0) -> bool:
    """指定 serial の adb 接続が実際に生きているか `shell echo` で確認する。"""
    try:
        r = subprocess.run(
            [ADB, "-s", serial, "shell", "echo", "ok"],
            capture_output=True, timeout=timeout,
        )
        return r.returncode == 0 and b"ok" in (r.stdout or b"")
    except Exception:
        return False


def adb_devices() -> list[tuple[str, str]]:
    """`adb devices` の結果を (serial, status) のリストで返す。"""
    try:
        r = subprocess.run(
            [ADB, "devices"],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        return []
    results: list[tuple[str, str]] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            results.append((parts[0], parts[1]))
    return results


async def _check_port(ip: str, port: int, timeout: float) -> int | None:
    try:
        fut = asyncio.open_connection(ip, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return port
    except Exception:
        return None


async def _scan_ports_async(ip: str, port_start: int, port_end: int,
                            concurrency: int, timeout: float,
                            on_progress: Callable[[int, int], None] | None) -> list[int]:
    sem = asyncio.Semaphore(concurrency)
    total = port_end - port_start + 1
    done = [0]
    step = max(1, total // 20)

    async def bounded(p: int) -> int | None:
        async with sem:
            r = await _check_port(ip, p, timeout)
            done[0] += 1
            if on_progress and done[0] % step == 0:
                on_progress(done[0], total)
            return r

    results = await asyncio.gather(*[bounded(p) for p in range(port_start, port_end + 1)])
    return [p for p in results if p is not None]


def scan_ports(ip: str, port_start: int = 30000, port_end: int = 65535,
               concurrency: int = 500, timeout: float = 0.3,
               on_progress: Callable[[int, int], None] | None = None) -> list[int]:
    """同期ラッパ: 指定 IP の TCP ポートを並列スキャンして開いているポートを返す。"""
    return asyncio.run(_scan_ports_async(ip, port_start, port_end,
                                         concurrency, timeout, on_progress))


def adb_mdns_services(timeout: float = 5.0) -> list[dict]:
    """`adb mdns services` の結果をパースして返す。

    Returns:
        [{"name": str, "service": str, "ip": str, "port": str}, ...]

    出力例: `adb-XXX-YYY\\t_adb-tls-connect._tcp\\t192.168.0.119:46821`

    取得できない場合はデバイスが mDNS で広告していない
    （画面オフ / 無線デバッグ無効 / 既に接続済みなど）。
    """
    # mDNS デーモンを起こして少し待つ（初回は populate 時間が必要）
    try:
        subprocess.run(
            [ADB, "mdns", "check"],
            capture_output=True, timeout=3,
        )
    except subprocess.TimeoutExpired:
        pass

    try:
        r = subprocess.run(
            [ADB, "mdns", "services"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return []

    results: list[dict] = []
    for raw in r.stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("List of") or line.startswith("mdns"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            parts = re.split(r"\s+", line, maxsplit=2)
        if len(parts) < 3:
            continue
        name = parts[0].strip()
        service = parts[1].strip()
        endpoint = parts[2].strip()
        if ":" in endpoint:
            ip, port = endpoint.rsplit(":", 1)
        else:
            ip, port = endpoint, ""
        results.append({"name": name, "service": service, "ip": ip, "port": port})
    return results


def adb_disconnect(serial: str, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            [ADB, "disconnect", serial],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode == 0, out or "(disconnected)"


def is_usb_serial(value: str) -> bool:
    """ドットを含まない = USB シリアル番号と判定する。"""
    return "." not in value


def connect_usb(serial: str, log_fn: Callable[[str], None] = print) -> tuple[bool, str, str]:
    """USB 接続済みデバイスの疎通確認。adb connect は不要なので ping のみ。"""
    log_fn(f"USB デバイス確認: {serial}")
    for s, status in adb_devices():
        if s == serial:
            if status != "device":
                return False, "", f"デバイス状態が '{status}' です（認証を許可してください）"
            if adb_ping(serial, timeout=3):
                log_fn(f"  USB 接続確認: {serial}")
                return True, serial, f"USB: {serial}"
            return False, "", f"デバイスは見えているが応答なし: {serial}"
    return False, "", f"adb devices に見つかりません: {serial}（ケーブルを確認してください）"


def discover_and_connect(ip: str, log_fn: Callable[[str], None] = print,
                         should_stop: Callable[[], bool] = lambda: False
                         ) -> tuple[bool, str, str]:
    """IP だけを与えてポートを自動検出し adb connect する。

    Returns:
        (success, serial, message)

    戦略:
        1. `adb devices` に既存の IP:* 接続があればそれを使う
        2. `adb mdns services` で該当 IP のポートを探して接続
        3. ポートスキャンで開いているポートを列挙し、adb connect で順に検証
    """
    # 0. 対象 IP の古いエントリを整理（offline / unauthorized / 死んでいる device）
    log_fn("(0/3) 既存エントリのクリーンアップ...")
    stale_cleared = 0
    for serial, status in adb_devices():
        if not serial.startswith(f"{ip}:"):
            continue
        if status == "device" and adb_ping(serial, timeout=2):
            # 生きている entry はそのまま残す（次ステップで再利用）
            continue
        # 死んでいる / offline / unauthorized 等は一掃
        log_fn(f"    stale: {serial} ({status}) -> disconnect")
        adb_disconnect(serial)
        stale_cleared += 1
    if stale_cleared:
        # adb server のキャッシュ反映待ち
        import time as _t
        _t.sleep(0.3)

    if should_stop():
        return False, "", "中断"

    # 1. 既存接続
    log_fn("(1/3) 既存の adb 接続を確認...")
    for serial, status in adb_devices():
        if serial.startswith(f"{ip}:") and status == "device":
            if adb_ping(serial, timeout=2):
                log_fn(f"    既存接続を再利用: {serial}")
                return True, serial, f"reuse existing: {serial}"
            log_fn(f"    {serial} は応答なし -> disconnect")
            adb_disconnect(serial)

    if should_stop():
        return False, "", "中断"

    # 2. mDNS
    log_fn("(2/3) mDNS 検出...")
    services = adb_mdns_services()
    # tls-connect を優先
    matched = [s for s in services if s.get("ip") == ip and s.get("port")]
    matched.sort(key=lambda s: 0 if "connect" in s.get("service", "") else 1)
    for s in matched:
        if should_stop():
            return False, "", "中断"
        port = s["port"]
        serial = f"{ip}:{port}"
        log_fn(f"    mDNS で候補発見: {serial} -> adb connect 試行")
        ok, out = adb_connect(serial, timeout=5)
        if ok:
            log_fn(f"    接続成功: {serial}")
            return True, serial, f"mdns -> {serial}"
        log_fn(f"    失敗: {out}")

    if should_stop():
        return False, "", "中断"

    # 3. ポートスキャン
    log_fn("(3/3) ポートスキャン開始 (30000-65535, ~30s)...")

    def _progress(done: int, total: int) -> None:
        log_fn(f"    scan {done}/{total}")

    open_ports = scan_ports(ip, on_progress=_progress)
    log_fn(f"    開いているポート: {len(open_ports)} 個")

    # adb connect の順序: wireless debug でよく使われる 30000〜45000 を優先
    def priority(p: int) -> int:
        if 30000 <= p <= 45000:
            return 0
        if 45001 <= p <= 55000:
            return 1
        return 2

    for port in sorted(open_ports, key=priority):
        if should_stop():
            return False, "", "中断"
        serial = f"{ip}:{port}"
        log_fn(f"    試行: {serial}")
        ok, out = adb_connect(serial, timeout=3)
        if ok:
            log_fn(f"    接続成功: {serial}")
            return True, serial, f"scan -> {serial}"

    return False, "", "どの方法でも接続できませんでした"


def launch_scrcpy(serial: str) -> subprocess.Popen:
    """scrcpy プレビューを別ウィンドウで起動する。"""
    return subprocess.Popen(
        [SCRCPY, "-s", serial,
         "--window-title", f"nightcrows preview ({serial})"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def get_rotation_and_size(serial: str) -> tuple[int, int, int]:
    """(rotation, phys_w, phys_h) を返す。tap_record.py と同じロジック。"""
    phys_w, phys_h = 1220, 2712
    r = subprocess.run(
        [ADB, "-s", serial, "shell", "wm", "size"],
        capture_output=True, text=True, timeout=5,
    )
    for line in r.stdout.splitlines():
        if "Physical size" in line or "Override size" in line:
            part = line.split(":")[-1].strip()
            try:
                phys_w, phys_h = map(int, part.split("x"))
            except ValueError:
                pass
            break

    rotation = 0
    r2 = subprocess.run(
        [ADB, "-s", serial, "shell", "dumpsys", "input"],
        capture_output=True, text=True, timeout=5,
    )
    for line in r2.stdout.splitlines():
        m = re.search(r"orientation=(\d+)", line)
        if m and "Viewport" in line:
            rotation = int(m.group(1))
            break
    return rotation, phys_w, phys_h
