"""OCR ヘルパー（モバイル版 flow_runner.py から最小限を移植）。"""
from __future__ import annotations

import cv2
import numpy as np


def _preprocess_for_ocr(crop: np.ndarray) -> list[np.ndarray]:
    """OCR用前処理バリアントを返す。

    バリアント順:
      0: Otsu 二値化
      1: Otsu 反転 — Otsu が明暗を誤判定したときの救済
      2: ぼかし後 Otsu — ノイズ・アンチエイリアスを平滑化
      3: 適応的二値化 — グラデーション背景に強い
    """
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, v0 = cv2.threshold(gray,    0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, v2 = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    v3 = cv2.adaptiveThreshold(
        blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )
    return [v0, cv2.bitwise_not(v0), v2, v3]


def ocr_digits_best(crop: np.ndarray, config: str) -> tuple[str | None, int]:
    """複数前処理で OCR を試み、最も長い数字列とバリアント番号を返す。"""
    try:
        import pytesseract
    except ImportError:
        return None, -1
    best_digits: str | None = None
    best_idx = -1
    for i, v in enumerate(_preprocess_for_ocr(crop)):
        try:
            text = pytesseract.image_to_string(v, config=config).strip()
            digits = "".join(ch for ch in text if ch.isdigit())
            if digits and (best_digits is None or len(digits) > len(best_digits)):
                best_digits = digits
                best_idx = i
        except Exception:
            continue
    return best_digits, best_idx
