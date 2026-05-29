"""自分側のウィンドウを退避してから `capture_window` を呼ぶヘルパー。

DirectX 系ゲーム（Nightcrows 等）では PrintWindow が空フレームを返し、
`capture_window` は画面 BitBlt にフォールバックする。このとき編集ウィンドウや
フロー本体が対象ゲーム画面上に重なっていると、その「映り込み」がスクショに
入ってしまう。

ここでは手動スクショ撮影など限定的な用途で、撮影中だけ自分側プロセスの
全ての可視 top-level widget を画面外へ移動し、撮影後に元の位置へ戻す。
ポーリング系（ウォッチャー監視・経験値 OCR・録画）には適用しない（毎回ちらつくため）。
"""
from __future__ import annotations

import time

import numpy as np
from PySide6.QtWidgets import QApplication

from .capture import capture_window


_PARK_X = -30000
_PARK_Y = -30000


def capture_window_clean(
    hwnd: int, settle_s: float = 0.15,
) -> np.ndarray | None:
    """`capture_window(hwnd)` と同じ戻り値。撮影中だけ自分のウィンドウを退避する。

    挙動:
        1. `QApplication.topLevelWidgets()` の中で `isVisible()` な物を抽出
        2. 全て (-30000, -30000) へ move（退避）
        3. `settle_s` 秒待ってデスクトップ再描画を待つ
        4. `capture_window(hwnd)` でキャプチャ
        5. finally で元の座標に戻す

    Qt のイベントループが回っていない呼び出し元（ない想定だが念のため）では、
    `processEvents` を明示的に挟む。

    Notes:
        - 退避中ユーザ操作（ドラッグ等）が走っていると挙動が乱れるが、手動スクショ
          時の僅かな瞬間なので許容する
        - 全ての可視 top-level widget が対象になるので、編集ウィンドウやフロー本体
          だけでなくダイアログ類も退避される（撮影完了後に復帰）
    """
    app = QApplication.instance()
    if app is None:
        # GUI 文脈外（テスト等）。素のキャプチャ。
        return capture_window(hwnd)

    moved: list[tuple] = []
    for w in app.topLevelWidgets():
        try:
            if not w.isVisible():
                continue
            moved.append((w, w.pos()))
            w.move(_PARK_X, _PARK_Y)
        except Exception:
            # widget 操作で例外が出ても他は試す
            pass

    if moved:
        app.processEvents()
        if settle_s > 0:
            time.sleep(settle_s)

    try:
        return capture_window(hwnd)
    finally:
        for w, p in moved:
            try:
                w.move(p)
            except Exception:
                pass
        if moved:
            app.processEvents()
