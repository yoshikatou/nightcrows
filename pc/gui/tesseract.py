"""Tesseract の自動検出とインストール案内。

設定不要で使えるようにするため、まずは既知のインストール先と PATH をスキャン。
見つからなければ案内ダイアログを表示する。
"""
from __future__ import annotations

import os
import shutil
import subprocess

_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
]

INSTALLER_URL = "https://github.com/UB-Mannheim/tesseract/wiki"
WINGET_CMD    = "winget install --id UB-Mannheim.TesseractOCR -e"


def detect_tesseract() -> str | None:
    """tesseract.exe のフルパスを返す。見つからなければ None。"""
    for p in _CANDIDATES:
        if p and os.path.isfile(p):
            return p
    p = shutil.which("tesseract")
    return p if p else None


def get_version(tess_path: str) -> str | None:
    """tesseract --version の最初の行を返す。失敗時 None。"""
    try:
        out = subprocess.run(
            [tess_path, "--version"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        line = (out.stdout or out.stderr or "").splitlines()
        return line[0].strip() if line else None
    except Exception:
        return None


def apply_path(tess_path: str | None) -> bool:
    """pytesseract にパスを適用。成功なら True。"""
    if not tess_path:
        return False
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = tess_path
        return True
    except ImportError:
        return False
