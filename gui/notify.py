"""デスクトップ通知ユーティリティ。バックグラウンドスレッドから安全に呼び出せる。"""
from __future__ import annotations

import subprocess
import sys
import threading


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
