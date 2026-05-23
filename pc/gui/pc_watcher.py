"""PC ウォッチャー: データモデル + 評価ロジック + JSON 入出力。

座標系は全てクライアント領域の相対比率 (0.0〜1.0)。

検知タイプ:
    image_appear: テンプレートが region 内で見つかったら発火（score >= threshold）
                  cv2.matchTemplate (TM_CCOEFF_NORMED)
    image_gone:   テンプレートが region 内で消えたら発火（score < threshold）
                  consecutive 回連続で未検出 → 最終発火（呼び出し側が管理）
    ocr_number:   region 内を OCR して読んだ数値が条件式を満たしたら発火
                  consecutive 回連続で条件成立した場合のみ最終発火（呼び出し側が管理）
"""
from __future__ import annotations

import json
import operator
import os
import re
import uuid
from dataclasses import dataclass, field

import cv2

from .ocr import ocr_digits_best

WATCHERS_DIR = "watchers"
WATCHER_TEMPLATES_DIR = "watcher_templates"

# 数値比較演算子（UI と JSON で共通）
OPS: dict[str, callable] = {
    "<":  operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    ">":  operator.gt,
}


@dataclass
class WatcherCondition:
    type: str = "image_appear"                  # "image_appear" | "ocr_number"
    template: str = ""                          # PNG パス（image_appear のみ）
    region: list[float] | None = None           # [rx, ry, rw, rh] 0.0〜1.0、None で画像全体
    threshold: float = 0.85                     # image_appear のマッチ閾値
    # OCR 数値判定用
    ocr_whitelist: str = "0123456789"           # 認識する文字種
    op: str = "<="                              # 比較演算子（OPS のキー）
    value: float = 0.0                          # 閾値（読み取った数値と比較）
    consecutive: int = 1                        # この回数連続でヒット → 発火


@dataclass
class PcWatcher:
    id: str = ""
    title: str = ""
    enabled: bool = True
    priority: int = 0
    condition: WatcherCondition = field(default_factory=WatcherCondition)
    handler: str = ""                           # 将来のフロー統合用（scenes/ 相対）
    after: str = "noop"                         # フロー統合フェーズで使用
    cooldown_s: float = 0.0
    alert_desktop: bool = False
    poll_min_s: float = 1.0
    poll_max_s: float = 4.0

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:8]


# ---------------------------------------------------------------- JSON 入出力
def _safe_filename(s: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_") or "watcher"
    return safe


def load_pc_watcher(path: str) -> PcWatcher:
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    c = d.get("condition", {}) or {}
    cond = WatcherCondition(
        type=c.get("type", "image_appear"),
        template=c.get("template", ""),
        region=list(c["region"]) if c.get("region") else None,
        threshold=float(c.get("threshold", 0.85)),
        ocr_whitelist=c.get("ocr_whitelist", "0123456789"),
        op=c.get("op", "<="),
        value=float(c.get("value", 0.0)),
        consecutive=int(c.get("consecutive", 1)),
    )
    return PcWatcher(
        id=d.get("id", ""),
        title=d.get("title", ""),
        enabled=bool(d.get("enabled", True)),
        priority=int(d.get("priority", 0)),
        condition=cond,
        handler=d.get("handler", ""),
        after=d.get("after", "noop"),
        cooldown_s=float(d.get("cooldown_s", 0.0)),
        alert_desktop=bool(d.get("alert_desktop", False)),
        poll_min_s=float(d.get("poll_min_s", 1.0)),
        poll_max_s=float(d.get("poll_max_s", 4.0)),
    )


def save_pc_watcher(w: PcWatcher, path: str | None = None) -> str:
    if path is None:
        name = _safe_filename(w.title or w.id)
        path = os.path.join(WATCHERS_DIR, f"{name}_{w.id}.json")
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    data = {
        "id": w.id,
        "title": w.title,
        "enabled": w.enabled,
        "priority": w.priority,
        "condition": {
            "type": w.condition.type,
            "template": w.condition.template,
            "region": list(w.condition.region) if w.condition.region else None,
            "threshold": w.condition.threshold,
            "ocr_whitelist": w.condition.ocr_whitelist,
            "op": w.condition.op,
            "value": w.condition.value,
            "consecutive": w.condition.consecutive,
        },
        "handler": w.handler,
        "after": w.after,
        "cooldown_s": w.cooldown_s,
        "alert_desktop": w.alert_desktop,
        "poll_min_s": w.poll_min_s,
        "poll_max_s": w.poll_max_s,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def list_pc_watchers() -> list[tuple[str, PcWatcher]]:
    """watchers/ 配下の全 JSON を読み込んで (path, watcher) のリストを返す。"""
    out: list[tuple[str, PcWatcher]] = []
    if not os.path.isdir(WATCHERS_DIR):
        return out
    for fname in sorted(os.listdir(WATCHERS_DIR)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(WATCHERS_DIR, fname)
        try:
            out.append((path, load_pc_watcher(path)))
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- 評価
@dataclass
class EvalResult:
    """ウォッチャー条件評価の結果。

    fired は「条件が単発で成立したか」を表す。最終発火（consecutive 回連続）の
    判定は呼び出し側が hit_count を管理して行う。
    """
    fired: bool = False
    score: float | None = None    # image_appear のマッチスコア
    value: float | None = None    # ocr_number で読み取った数値
    raw: str = ""                 # OCR の生テキスト or エラー理由
    note: str = ""                # 補足（人間向け短文）


def _crop_region(img, region: list[float] | None):
    if img is None:
        return None
    if not region or len(region) != 4:
        return img
    ih, iw = img.shape[:2]
    rx, ry, rw, rh = region
    x0 = max(0, int(rx * iw))
    y0 = max(0, int(ry * ih))
    x1 = min(iw, int((rx + rw) * iw))
    y1 = min(ih, int((ry + rh) * ih))
    if x1 <= x0 or y1 <= y0:
        return None
    return img[y0:y1, x0:x1]


def _evaluate_image_appear(img, w: PcWatcher) -> EvalResult:
    c = w.condition
    if not c.template or not os.path.exists(c.template):
        return EvalResult(note="テンプレート未設定")
    tmpl = cv2.imread(c.template, cv2.IMREAD_COLOR)
    if tmpl is None or img is None:
        return EvalResult(note="画像読み込み失敗")
    target = _crop_region(img, c.region)
    if target is None:
        return EvalResult(note="領域不正")
    if (target.shape[0] < tmpl.shape[0]
            or target.shape[1] < tmpl.shape[1]):
        return EvalResult(note="領域がテンプレより小さい")
    res = cv2.matchTemplate(target, tmpl, cv2.TM_CCOEFF_NORMED)
    _, maxv, _, _ = cv2.minMaxLoc(res)
    return EvalResult(fired=(maxv >= c.threshold), score=float(maxv))


def _evaluate_ocr_number(img, w: PcWatcher) -> EvalResult:
    c = w.condition
    target = _crop_region(img, c.region)
    if target is None or target.size == 0:
        return EvalResult(note="領域不正")
    crop_h, crop_w = target.shape[:2]
    whitelist = c.ocr_whitelist or "0123456789"

    # 複数 PSM (page segmentation mode) を試して最も長い数字列を採用:
    #   PSM 7 = 1 行のテキスト / 8 = 単語 1 個 / 6 = 均一なブロック
    best_digits: str | None = None
    best_psm = -1
    best_var = -1
    for psm in (7, 8, 6):
        config = f"--psm {psm} --oem 3 -c tessedit_char_whitelist={whitelist}"
        digits, var = ocr_digits_best(target, config)
        if digits and (best_digits is None or len(digits) > len(best_digits)):
            best_digits = digits
            best_psm = psm
            best_var = var

    debug = f"crop={crop_w}x{crop_h} psm={best_psm} var={best_var}"
    if not best_digits:
        return EvalResult(note=f"OCR読取失敗 [{debug}]")
    try:
        v = float(best_digits)
    except ValueError:
        return EvalResult(raw=best_digits, note=f"数値変換失敗 [{debug}]")
    op_fn = OPS.get(c.op)
    if op_fn is None:
        return EvalResult(value=v, raw=best_digits, note=f"未知の演算子 {c.op} [{debug}]")
    fired = bool(op_fn(v, c.value))
    return EvalResult(fired=fired, value=v, raw=best_digits, note=debug)


def _evaluate_image_gone(img, w: PcWatcher) -> EvalResult:
    """image_appear と同じスコア計算をして、threshold 未満なら fired=True にする。"""
    r = _evaluate_image_appear(img, w)
    if r.score is None:
        return r
    fired_gone = r.score < w.condition.threshold
    return EvalResult(fired=fired_gone, score=r.score, note=r.note)


def evaluate_watcher(img, watcher: PcWatcher) -> EvalResult:
    """ウォッチャー条件を1回評価する。連続ヒット (consecutive) は呼び出し側で管理。"""
    t = watcher.condition.type
    if t == "image_appear":
        return _evaluate_image_appear(img, watcher)
    if t == "image_gone":
        return _evaluate_image_gone(img, watcher)
    if t == "ocr_number":
        return _evaluate_ocr_number(img, watcher)
    return EvalResult(note=f"未知の検知タイプ {t}")
