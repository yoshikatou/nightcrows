"""デスクトップ通知 + Google Chat Webhook ユーティリティ。

バックグラウンドスレッドから安全に呼び出せる。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime


def show_desktop_alert(title: str, body: str = "") -> None:
    """Windows トースト/バルーン通知を非同期で表示する。失敗は無視する。"""
    if sys.platform != "win32":
        return

    safe_title = title.replace("'", "\\'")
    safe_body  = body.replace("'", "\\'")

    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Warning;"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip(8000, '{safe_title}', '{safe_body}', "
        "[System.Windows.Forms.ToolTipIcon]::Warning);"
        "Start-Sleep -Milliseconds 8500;"
        "$n.Dispose()"
    )

    threading.Thread(
        target=lambda: subprocess.run(
            ["powershell", "-WindowStyle", "Hidden", "-NonInteractive", "-Command", ps],
            capture_output=True,
        ),
        daemon=True,
    ).start()


# ---------------------------------------------------------- Google Chat Webhook
def _post_json(url: str, payload: dict, timeout: float = 10.0) -> tuple[bool, str]:
    if not url:
        return False, "Webhook URL が設定されていません"
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=UTF-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return False, f"HTTP {e.code}: {body}"
    except urllib.error.URLError as e:
        return False, f"URL エラー: {e.reason}"
    except Exception as e:
        return False, f"例外: {e}"


def send_google_chat(
    webhook_url: str,
    title: str,
    body: str,
    when: datetime | None = None,
) -> tuple[bool, str]:
    """Google Chat に通知を送る。

    本文フォーマット:
        *<title>*
        🕒 YYYY-MM-DD HH:MM:SS
        <body>

    戻り値: (成功か, メッセージ)。
    """
    when = when or datetime.now()
    ts = when.strftime("%Y-%m-%d %H:%M:%S")
    text = f"*{title}*\n🕒 {ts}\n{body}"
    return _post_json(webhook_url, {"text": text})


def send_google_chat_async(
    webhook_url: str, title: str, body: str,
    on_done: "callable | None" = None,
) -> None:
    """別スレッドで Google Chat 通知を送る。失敗時は on_done(False, msg) で通知。"""
    if not webhook_url:
        return

    def _worker() -> None:
        ok, msg = send_google_chat(webhook_url, title, body)
        if on_done:
            try:
                on_done(ok, msg)
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()
