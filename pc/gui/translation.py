"""チャット領域・ユーザー入力テキストを Claude API で翻訳するクライアント。

スクショ送信:
    Anthropic SDK の vision 機能で画像 (PNG bytes) を投げ、チャット行ごとに
    {言語コード, 原文, 翻訳} のリストを JSON で返してもらう。

ユーザー入力翻訳:
    text を Claude に投げ、指定の対象言語へ翻訳した辞書を返してもらう。

依存: `anthropic` パッケージ (`pip install anthropic`)
"""
from __future__ import annotations

import base64
import json
import re

LANG_CODES = ["ja", "en", "zh", "ko", "th", "tl"]
LANG_NAMES = {
    "ja": "Japanese",
    "en": "English",
    "zh": "Chinese (Simplified)",
    "ko": "Korean",
    "th": "Thai",
    "tl": "Tagalog (Filipino)",
}
LANG_LABELS_JA = {
    "ja": "日本語",
    "en": "英語",
    "zh": "中国語",
    "ko": "韓国語",
    "th": "タイ語",
    "tl": "タガログ語",
}

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1500


def _parse_json_block(text: str) -> dict | list | None:
    """応答テキストから JSON 部分を抽出してパースする。

    Claude が説明文を付けた場合も最初の {...} or [...] ブロックだけ拾う。
    失敗時 None。
    """
    if not text:
        return None
    # まず全体を試す
    try:
        return json.loads(text)
    except Exception:
        pass
    # コードフェンス内の JSON
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 単純な {…} / […] の最初のブロック
    m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None


class TranslationClient:
    """Claude API への翻訳呼び出しラッパー。

    `anthropic` パッケージが未インストールならインスタンス化で ImportError。
    `api_key` が空文字なら最初の API 呼び出しで RuntimeError。
    """

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise ImportError(
                "anthropic パッケージが必要です: `pip install anthropic`"
            ) from e
        self._anthropic = anthropic
        self._api_key = (api_key or "").strip()
        self._model = model
        self._client = None
        if self._api_key:
            self._client = anthropic.Anthropic(api_key=self._api_key)

    def is_configured(self) -> bool:
        return bool(self._api_key)

    # ------------------------------------------------------ チャット領域翻訳
    def translate_image(
        self, png_bytes: bytes, base_lang: str,
    ) -> list[dict]:
        """チャット画像を解析し、各メッセージの (lang, original, translated) を返す。

        base_lang は ja/en/zh/ko/th/tl のいずれか。base_lang と同じ言語のメッセージは
        translated=None で返る（翻訳不要）。

        戻り値: [{"lang": "en", "original": "Hello", "translated": "こんにちは"}, ...]
        画像が読めない・チャットが無い場合は []。
        """
        if not self._client:
            raise RuntimeError("API キー未設定")
        base_name = LANG_NAMES.get(base_lang, LANG_NAMES["ja"])
        prompt = (
            "This image is a screenshot of a game chat panel. Multiple users may be "
            "chatting in different languages (Japanese, English, Chinese, Korean, "
            "Thai, Tagalog). \n\n"
            "TASK:\n"
            "1. Identify each visible chat message (one message per line of dialogue).\n"
            "2. Detect the language of each message — use one of these ISO codes: "
            "ja, en, zh, ko, th, tl.\n"
            f"3. If the message language is NOT \"{base_lang}\", translate it into "
            f"{base_name}. Skip translation if the message is already in {base_name}.\n"
            "4. Ignore user names, timestamps, and UI elements — output only the "
            "message text.\n\n"
            "Respond strictly in this JSON format (no markdown, no commentary):\n"
            "{\n"
            "  \"messages\": [\n"
            "    {\"lang\": \"<code>\", \"original\": \"<text>\", "
            "\"translated\": \"<text or null>\"}\n"
            "  ]\n"
            "}\n\n"
            "If no chat text is visible, respond: {\"messages\": []}"
        )
        b64 = base64.standard_b64encode(png_bytes).decode("ascii")
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text = "".join(
            getattr(b, "text", "") for b in resp.content
            if getattr(b, "type", "") == "text"
        )
        data = _parse_json_block(text)
        if not isinstance(data, dict):
            return []
        msgs = data.get("messages")
        if not isinstance(msgs, list):
            return []
        out: list[dict] = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            lang = str(m.get("lang", "")).strip().lower()
            original = str(m.get("original", "")).strip()
            translated = m.get("translated")
            if translated is not None:
                translated = str(translated).strip() or None
            if not original:
                continue
            out.append({
                "lang": lang or "?",
                "original": original,
                "translated": translated,
            })
        return out

    # ------------------------------------------------------ ユーザー入力翻訳
    def translate_text(self, text: str, targets: list[str]) -> dict[str, str]:
        """`text` を `targets` の各言語に翻訳して辞書で返す。

        戻り値: {"en": "Hello", "zh": "你好", ...}
        翻訳に失敗した言語は欠落する。
        """
        if not self._client:
            raise RuntimeError("API キー未設定")
        if not text.strip():
            return {}
        if not targets:
            return {}
        targets_str = ", ".join(
            f"{code} ({LANG_NAMES.get(code, code)})" for code in targets if code in LANG_NAMES
        )
        prompt = (
            "Translate the following text into each of these target languages. "
            "Preserve tone and meaning naturally — do not transliterate literally.\n\n"
            f"Target languages: {targets_str}\n\n"
            f"Source text:\n{text}\n\n"
            "Respond strictly in JSON (no markdown, no commentary). "
            "Use language codes as keys (e.g. en, zh, ko, th, tl, ja). "
            "Example:\n"
            "{\n"
            "  \"en\": \"Hello\",\n"
            "  \"zh\": \"你好\"\n"
            "}"
        )
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        body = "".join(
            getattr(b, "text", "") for b in resp.content
            if getattr(b, "type", "") == "text"
        )
        data = _parse_json_block(body)
        if not isinstance(data, dict):
            return {}
        out: dict[str, str] = {}
        for code in targets:
            v = data.get(code)
            if isinstance(v, str) and v.strip():
                out[code] = v.strip()
        return out
