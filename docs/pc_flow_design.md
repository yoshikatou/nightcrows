# PC フロー制御プログラム 設計書

作成: 2026-05-22  
ステータス: **設計完了・実装待ち**

## 概要

モバイル版フローシステム（ADB + Android）の PC 強化版。  
既存の経験値計測（`pc/run_exp_meter.py`）はコンパクト版として**変更なし**で残す。

| 項目 | コンパクト版（既存） | PC フロー版（新規） |
|------|---------------------|-------------------|
| エントリー | `pc/run_exp_meter.py` | `pc/run_pc_flow.py` |
| 主機能 | 経験値計測のみ | フロー自動実行 + 経験値計測 |
| 操作手段 | Win32 キャプチャ + OCR | Pico HID マウス + Win32 キャプチャ |

---

## ファイル構成

```
pc/
├── run_pc_flow.py           # エントリーポイント（新規）
├── scenes/                  # PC シーン定義（新規）
│   └── BF3_92LV.json
├── flows/                   # PC フロー定義（新規）
│   └── 基本_pc.json
├── templates/               # PC テンプレート画像（新規）
└── gui/
    ├── pc_scene.py          # シーン実行エンジン（新規）
    ├── pc_flow.py           # スケジューラー（新規）
    ├── pc_main.py           # メインウィンドウ（新規）
    │
    │   ── 既存（変更なし） ──
    ├── exp_meter.py         # 経験値計測コア（再利用）
    ├── capture.py           # Win32 キャプチャ（再利用）
    ├── window_picker.py     # ウィンドウ選択（再利用）
    └── main.py              # コンパクト版（そのまま）
```

---

## PC シーン JSON フォーマット

モバイル版との主な違い: **絶対 px → ウィンドウ相対比率（0.0〜1.0）**

```json
{
  "name": "BF3 92LV",
  "window_title": "NIGHT CROWS",
  "steps": [
    {"type": "snapshot", "path": "pc/templates/snap_001.png", "timeout_s": 10},
    {"type": "tap",   "rx": 0.49, "ry": 0.055, "duration_ms": 100},
    {"type": "wait_fixed", "seconds": 1.5},
    {"type": "swipe", "rx1": 0.89, "ry1": 0.40, "rx2": 0.89, "ry2": 0.12, "duration_ms": 600}
  ]
}
```

### モバイルとの対応

| モバイル | PC |
|---------|-----|
| `tap` x/y (device px) | `tap` rx/ry (ウィンドウ相対比率) |
| `swipe` x1/y1/x2/y2 | `swipe` rx1/ry1/rx2/ry2 |
| `snapshot` → ADB screencap | `snapshot` → Win32 PrintWindow |
| ADB `input tap` | Pico HID クリック |
| ADB `input swipe` | Pico press + move_to + release |

---

## 実行フロー

```
PcSceneRunner.run(scene_json)
  ↓ ステップごとに:
  "snapshot"   → capture_window(hwnd)
                 → cv2.matchTemplate()
                 → 一致しなければリトライ / タイムアウトで失敗
  "tap"        → (rx, ry) → 絶対スクリーン座標変換
                 → PicoMouse.move_to(abs_x, abs_y)
                 → PicoMouse.click()
  "swipe"      → PicoMouse.press()
                 → PicoMouse.move_to(x2, y2)
                 → PicoMouse.release()
  "wait_fixed" → time.sleep(seconds)
```

### 座標変換

```python
# ゲームウィンドウのクライアント領域を取得
win_rect = win32gui.GetClientRect(hwnd)         # (0,0,w,h)
origin = win32gui.ClientToScreen(hwnd, (0, 0))  # 画面上の左上座標
abs_x = origin[0] + rx * win_rect[2]
abs_y = origin[1] + ry * win_rect[3]
```

---

## フロー JSON フォーマット

モバイル版（`mobile/flows/*.json`）と同じスケジュール形式を流用。

```json
{
  "name": "基本_pc",
  "version": 1,
  "schedule": [
    {
      "time": "08:30",
      "target": "BF3_92LV.json",
      "repeat": "weekly",
      "days": [1, 2, 3]
    }
  ],
  "watchers": [],
  "settings": {
    "polling_interval_s": 1.0
  }
}
```

---

## UI レイアウト（`pc_main.py`）

```
┌─────────────────────────────────────────────────┐
│ PC フロー制御                                    │
│                                                 │
│ ゲームウィンドウ: [NIGHT CROWS       ▼] [更新]  │
│ Pico マウス:    [接続済 COM5 ✓]  [再接続]      │
│                                                 │
│ フロー: [基本_pc.json ▼]  [開始] [停止]        │
│ 状態: 待機中 / 実行中: BF3 92LV ステップ 3/12  │
│                                                 │
│ ─── スケジュール ──────────────────────────────│
│ 08:30 BF3 92LV (月〜水)                        │
│ 13:30 BF3 92LV (木・金)    ← 次回まで 2h15m   │
│                                                 │
│ ─── 経験値メーター ────────────────────────────│
│ 現在 58.80%  速度 +0.62%/h  累計 +1.84%       │
│ [計測開始] [リセット]                           │
└─────────────────────────────────────────────────┘
```

---

## 実装順序（推奨）

1. `pc/gui/pc_scene.py` — シーン実行エンジン（コア）
2. `pc/gui/pc_flow.py` — スケジューラー
3. `pc/gui/pc_main.py` — メインウィンドウ
4. `pc/run_pc_flow.py` — エントリーポイント
5. 座標記録ツール — move_test に追加（オプション9: ゲームウィンドウ相対座標キャプチャ）
6. PC 用シーン JSON 作成（ゲームを見ながら記録）

---

## 依存関係

- `pc/pico_mouse.py` — PicoMouse クラス（実装済み）
- `pc/gui/capture.py` — capture_window（実装済み）
- `pc/gui/exp_meter.py` — ExpMeter クラス（実装済み）
- `pc/gui/window_picker.py` — find_hwnd_by_title（実装済み）
- OpenCV (`cv2`) — テンプレートマッチング
- PySide6 — UI
- win32gui, win32con — ウィンドウ操作
