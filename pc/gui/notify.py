"""通知ヘルパー（Google Chat Incoming Webhook）。

Google Chat の Webhook は単純な JSON POST。フォーマットは text フィールドに
Markdown 風記法（*太字*、_斜体_、\\n 改行）が使える。

依存を増やしたくないので urllib のみで実装する（requests 等は使わない）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> tuple[bool, str]:
    """JSON を POST し、(成功か, ステータス/エラー文字列) を返す。"""
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

    戻り値: (成功か, メッセージ)。失敗時もメッセージで原因が分かる。
    """
    when = when or datetime.now()
    ts = when.strftime("%Y-%m-%d %H:%M:%S")
    text = f"*{title}*\n🕒 {ts}\n{body}"
    return _post_json(webhook_url, {"text": text})
