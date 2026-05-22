# PC フロー制御システム 設計書

作成: 2026-05-22  
更新: 2026-05-22  
ステータス: **実装完了（シーン JSON 作成待ち）**

---

## 1. 概要

Night Crows PC 版の定期操作（BF3・BR・クエスト等）を時刻スケジュールで自動実行するシステム。

| 項目 | 内容 |
|------|------|
| 入力手段 | Raspberry Pi Pico USB HID マウス（`PicoMouse`） |
| 画面認識 | Win32 PrintWindow キャプチャ + OpenCV テンプレートマッチング |
| 操作定義 | JSON ファイル（シーン・フロー） |
| UI | PySide6 デスクトップ GUI |
| 起動 | `python pc/run_pc_flow.py` |

---

## 2. ファイル構成

```
D:/github2/nightcrows/
├── pc/
│   ├── run_pc_flow.py          # エントリーポイント
│   ├── pico_mouse.py           # Pico HID マウスコントローラ（既存）
│   ├── scenes/                 # PC シーン JSON（要作成）
│   │   └── BF3_92LV.json
│   ├── flows/                  # PC フロー JSON
│   │   └── 基本_pc.json
│   └── gui/
│       ├── pc_scene.py         # シーン実行エンジン       ← 今回追加
│       ├── pc_flow.py          # スケジューラー           ← 今回追加
│       ├── pc_main.py          # メインウィンドウ         ← 今回追加
│       │    ── 既存（変更なし） ──
│       ├── capture.py          # Win32 キャプチャ
│       ├── exp_meter.py        # 経験値計測コア
│       ├── window_picker.py    # ウィンドウ選択ダイアログ
│       ├── settings.py         # settings.json 読み書き
│       ├── main.py             # 経験値メーター単体（コンパクト版）
│       └── overlay.py          # オーバーレイウィンドウ
└── docs/
    ├── pc_flow_design.md       # 当初設計書（構想フェーズ）
    └── pc_flow_system.md       # 本設計書（実装後）
```

---

## 3. アーキテクチャ

### 3.1 レイヤー構成

```
┌──────────────────────────────────────────────┐
│  pc_main.py  PcFlowWindow (PySide6 GUI)      │  プレゼンテーション層
│  ・ウィンドウ選択   ・Pico 接続              │
│  ・フロー開始/停止  ・経験値メーター表示      │
└──────────────────────────────────────────────┘
          ↕ Qt Signal/Slot
┌──────────────────────────────────────────────┐
│  pc_flow.py  PcFlowRunner (QObject)          │  スケジューリング層
│  ・時刻スケジュール評価                       │
│  ・シーンシーケンス実行                       │
│  ・スレッド管理                               │
└──────────────────────────────────────────────┘
          ↓ 呼び出し
┌──────────────────────────────────────────────┐
│  pc_scene.py  run_pc_scene()                 │  シーン実行層
│  ・ステップ解釈・実行                         │
│  ・座標変換（相対比率 → 絶対 px）             │
└──────────────────────────────────────────────┘
          ↓ 使用
┌────────────────────┐  ┌───────────────────────┐
│  pico_mouse.py     │  │  capture.py / cv2     │
│  PicoMouse         │  │  Win32 キャプチャ     │
│  HID クリック/移動 │  │  テンプレートマッチ   │
└────────────────────┘  └───────────────────────┘
```

### 3.2 スレッドモデル

```
メインスレッド (Qt GUI)
  ├── PcFlowWindow  ─── QTimer (1秒) ─→ _refresh_status()
  └── PcFlowRunner
        └── バックグラウンドスレッド (daemon=True)
              ├── check_schedule() をポーリング
              └── _run_scene() → run_pc_scene() → PicoMouse 操作
                  ※ Qt シグナルは emit() でメインスレッドへ安全に伝達
```

---

## 4. コンポーネント詳細

### 4.1 pc_scene.py — シーン実行エンジン

#### データモデル

```python
@dataclass
class PcStep:
    type: str           # "tap" | "swipe" | "snapshot" | "wait_fixed"
    params: dict        # ステップ固有パラメータ

@dataclass
class PcScene:
    name: str           # 表示名
    window_title: str   # 対象ゲームウィンドウのタイトル（部分一致）
    steps: list[PcStep]
```

#### 主要関数

| 関数 | 説明 |
|------|------|
| `load_pc_scene(path)` | JSON ファイルから PcScene を読み込む |
| `save_pc_scene(scene, path)` | PcScene を JSON ファイルに保存 |
| `rel_to_abs(hwnd, rx, ry)` | ウィンドウ相対比率 → 絶対スクリーン座標 |
| `run_pc_scene(scene, mouse, hwnd, log, should_stop, step_callback)` | シーン全体を実行。成功→True、中断/失敗→False |

#### ステップ実行詳細

**`snapshot`** — ゲーム画面が期待状態になるまで待機

```
capture_window(hwnd)
  → cv2.matchTemplate(img, tmpl, TM_CCOEFF_NORMED)
  → score >= threshold なら通過
  → deadline 超過でシーン失敗 (return False)
  ポーリング間隔: 0.5 秒
```

**`tap`** — 指定座標をクリック

```
(rx, ry) → rel_to_abs(hwnd, rx, ry) → (abs_x, abs_y)
PicoMouse.click(abs_x, abs_y, button, hold_ms)
  ※ SetCursorPos でカーソル移動 + Pico HID でボタン押下
```

**`swipe`** — ドラッグ操作

```
(rx1,ry1) → abs (x1,y1)
(rx2,ry2) → abs (x2,y2)

dist = max(|x2-x1|, |y2-y1|)
n_steps  = max(5, dist // 15)
step_delay = duration_ms / 1000 / n_steps
max_step = dist // n_steps + 1

PicoMouse.move_cursor(x1, y1)   # SetCursorPos で開始点へ
PicoMouse.press("L")
PicoMouse.move_to(x2, y2, max_step, delay=step_delay)  # HID イーズアウト
PicoMouse.release()
```

**`wait_fixed`** — 固定時間待機（0.05 秒刻みで停止確認）

#### 座標変換式

```python
rect   = win32gui.GetClientRect(hwnd)    # → (0, 0, width, height)
origin = win32gui.ClientToScreen(hwnd, (0, 0))
abs_x  = origin[0] + rx * rect[2]
abs_y  = origin[1] + ry * rect[3]
```

ゲームウィンドウのサイズ変更・移動に毎回追従する。

---

### 4.2 pc_flow.py — スケジューラー

#### データモデル

```python
@dataclass
class ScheduleEntry:
    time: str           # "HH:MM"
    target: str         # 実行シーンファイル名（sequence 空のとき使用）
    sequence: list[str] # 順番に実行するシーンファイル名リスト
    repeat: str         # "daily" | "weekly" | "once"
    days: list[int]     # 0=月〜6=日（repeat="weekly" のとき使用）
    date: str           # "YYYY-MM-DD"（repeat="once" のとき使用）
    enabled: bool       # False にするとスキップ

@dataclass
class FlowSettings:
    polling_interval_s: float  # スケジュール確認間隔（秒）

@dataclass
class PcFlow:
    name: str
    version: int
    schedule: list[ScheduleEntry]
    settings: FlowSettings
```

#### 主要関数

| 関数 | 説明 |
|------|------|
| `load_pc_flow(path)` | JSON から PcFlow を読み込む |
| `save_pc_flow(flow, path)` | PcFlow を JSON に保存 |
| `entry_scenes(entry)` | target / sequence の混在を吸収してシーンリストを返す |
| `check_schedule(flow, now, last_fired)` | 現時刻で発火すべきエントリを返す（無ければ None） |
| `next_schedule_str(flow, now)` | 次回発火予定の説明文を返す（UI 表示用） |

#### PcFlowRunner (QObject)

**Qt シグナル**

| シグナル | 型 | タイミング |
|----------|-----|-----------|
| `log_message` | `str` | ログ行が出るたびに |
| `scene_started` | `str, int, int` | シーン開始時 (name, step, total) |
| `step_updated` | `int, int` | ステップ完了時 (current, total) |
| `state_changed` | `str` | "running" / "idle" に切り替わった時 |
| `next_schedule_changed` | `str` | 次回予定の説明文が変化した時 |

**公開 API**

```python
runner = PcFlowRunner()
runner.set_mouse(pico_mouse)          # PicoMouse インスタンスをセット
runner.set_window_title("NIGHT CROWS")
flow = runner.load_flow("flows/基本_pc.json")  # → PcFlow
runner.start()                        # バックグラウンドスレッド開始
runner.stop()                         # 停止要求（次のステップ境界で終了）
runner.is_running                     # bool
runner.current_scene                  # str（実行中シーン名）
runner.current_step                   # (int, int)
```

**スケジュール発火ロジック**

```
起動時:
  現時刻より前のエントリをすべて last_fired に登録（重複実行防止）

ポーリングループ:
  check_schedule(flow, now, last_fired)
    条件: entry.time <= now.HH:MM
          repeat="weekly" → today_weekday in entry.days
          repeat="once"   → entry.date == today
          last_fired[idx] != today（当日発火済みでない）
  → 発火エントリを時刻昇順でソート → 最古を1件返す

発火時:
  last_fired[idx] = today
  entry_scenes(entry) のシーンを順次 _run_scene() で実行
  完了後 next_schedule_changed を emit
```

---

### 4.3 pc_main.py — メインウィンドウ

#### ウィジェット構成

```
PcFlowWindow (QWidget)
├── QGroupBox "ゲームウィンドウ"
│   ├── QLabel  lbl_win   (✓/⚠/未設定 + タイトル)
│   └── QPushButton btn_win  "選択…" → WindowPickerDialog
│
├── QGroupBox "Pico マウス"
│   ├── QLabel  lbl_pico  (✓接続済 / ✗失敗)
│   └── QPushButton btn_pico "接続" / "再接続"
│
├── QGroupBox "フロー"
│   ├── QComboBox  combo_flow  (flows/*.json 一覧)
│   ├── QPushButton btn_flow   "開始" / "停止"（赤）
│   ├── QLabel  lbl_run_status  "待機中" / "実行中: シーン名 ステップ N/M"
│   ├── QListWidget list_sched  スケジュール一覧
│   └── QLabel  lbl_next_sched  "次回: HH:MM シーン名 (曜日) 残り Xh Ym"
│
├── QGroupBox "経験値メーター"
│   ├── QLabel lbl_exp_cur   "現在値: XX.XXXX%"
│   ├── QLabel lbl_exp_spd   "速度: +X.XX %/h"
│   ├── QLabel lbl_exp_acc   "累計: +X.XXXX%"
│   ├── QPushButton btn_exp  "計測開始" / "計測停止"（赤）
│   └── QPushButton btn_exp_reset "リセット"
│
└── QGroupBox "ログ"
    └── QTextEdit log_box  (読み取り専用、自動スクロール)
```

#### 起動シーケンス

```
__init__()
  1. _build_ui()           ウィジェット生成
  2. _connect_signals()    Qt シグナルを接続
  3. _setup_meter()        古いログを削除
  4. _load_flows_list()    flows/ ディレクトリを読んで combo_flow を埋める
  5. _restore_settings()   settings.json から前回状態を復元
  6. QTimer.singleShot(400ms) → _auto_connect_pico()
  7. QTimer(1000ms)        → _refresh_status() を定期呼び出し
```

#### settings.json で保存する項目

| キー | 型 | 説明 |
|------|----|------|
| `window_title` | str | 最後に選択したゲームウィンドウタイトル |
| `last_flow` | str | 最後に選択したフロー JSON ファイル名 |
| `region_rel` | list[float] | 経験値メーター計測領域（rx, ry, rw, rh） |
| `digit_hint` | int | 経験値 OCR の桁数ヒント（1 or 2） |
| `tesseract_cmd` | str | Tesseract 実行ファイルパス（空=自動検出） |
| `log_retain_days` | int | ログ自動削除の日数 |

---

### 4.4 run_pc_flow.py — エントリーポイント

```python
_ensure_cwd()   # exe / スクリプトのディレクトリを CWD にセット
from gui.pc_main import main
main()
```

`os.chdir()` で CWD を `pc/` に揃えることで、`scenes/`・`flows/`・`settings.json`・`logs/` がすべて `pc/` 直下で解決される。PyInstaller バンドル時も `sys.frozen` フラグで判定して正しく動作する。

---

## 5. JSON フォーマット

### 5.1 シーン JSON（pc/scenes/*.json）

```json
{
  "name": "BF3 92LV",
  "window_title": "NIGHT CROWS",
  "steps": [
    {
      "type": "snapshot",
      "path": "templates/BF3_92LV_start.png",
      "timeout_s": 15,
      "threshold": 0.85
    },
    {
      "type": "tap",
      "rx": 0.49,
      "ry": 0.055,
      "button": "L",
      "duration_ms": 50
    },
    {
      "type": "wait_fixed",
      "seconds": 1.5
    },
    {
      "type": "swipe",
      "rx1": 0.89,
      "ry1": 0.40,
      "rx2": 0.89,
      "ry2": 0.12,
      "duration_ms": 600
    }
  ]
}
```

#### ステップパラメータ一覧

| type | パラメータ | 型 | デフォルト | 説明 |
|------|-----------|-----|-----------|------|
| `snapshot` | `path` | str | 必須 | テンプレート画像パス（CWD 相対） |
| | `timeout_s` | float | 10.0 | 一致待ちのタイムアウト秒 |
| | `threshold` | float | 0.85 | TM_CCOEFF_NORMED の最低スコア |
| `tap` | `rx`, `ry` | float | 0.5 | クリック座標（ウィンドウ相対比率 0.0〜1.0） |
| | `button` | str | "L" | "L" / "R" / "M" |
| | `duration_ms` | int | 50 | ボタン押下時間（ms） |
| `swipe` | `rx1`, `ry1` | float | 0.5 | 開始座標（ウィンドウ相対比率） |
| | `rx2`, `ry2` | float | 0.5 | 終了座標（ウィンドウ相対比率） |
| | `duration_ms` | int | 500 | スワイプ所要時間（ms） |
| `wait_fixed` | `seconds` | float | 1.0 | 待機秒数 |

### 5.2 フロー JSON（pc/flows/*.json）

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
    },
    {
      "time": "13:30",
      "target": "BF3_92LV.json",
      "repeat": "weekly",
      "days": [3, 4]
    },
    {
      "time": "05:30",
      "sequence": ["シーン1.json", "シーン2.json"],
      "repeat": "daily"
    }
  ],
  "watchers": [],
  "settings": {
    "polling_interval_s": 1.0
  }
}
```

#### ScheduleEntry パラメータ

| キー | 型 | 説明 |
|------|----|------|
| `time` | str | 発火時刻 "HH:MM" |
| `target` | str | 実行シーンファイル名（sequence が空のとき使用） |
| `sequence` | list[str] | 順次実行するシーンのリスト |
| `repeat` | str | "daily" / "weekly" / "once" |
| `days` | list[int] | 曜日（0=月〜6=日）repeat="weekly" のとき |
| `date` | str | "YYYY-MM-DD" repeat="once" のとき |
| `enabled` | bool | false でスキップ（省略時 true） |

**target と sequence の優先規則:**
- `sequence` が空 → `[target]` を使用
- `sequence` に `target` が含まれていない → `[target] + sequence`
- それ以外 → `sequence` をそのまま使用

---

## 6. 依存関係

### 6.1 モジュール依存図

```
run_pc_flow.py
    └── gui.pc_main
            ├── gui.pc_flow
            │       └── gui.pc_scene
            │               ├── gui.capture
            │               └── gui.window_picker
            ├── gui.exp_meter
            │       ├── gui.capture
            │       ├── gui.ocr (Tesseract)
            │       └── gui.window_picker
            ├── gui.settings
            ├── gui.window_picker
            └── pico_mouse          ← pc/ 直下（try/except でオプション扱い）
```

### 6.2 外部ライブラリ

| ライブラリ | 用途 | インストール |
|-----------|------|------------|
| PySide6 | GUI フレームワーク | `pip install PySide6` |
| opencv-python | テンプレートマッチング | `pip install opencv-python` |
| pywin32 | Win32 API (win32gui, win32con, win32ui) | `pip install pywin32` |
| pyserial | Pico とのシリアル通信 | `pip install pyserial` |
| pytesseract | 経験値 OCR | `pip install pytesseract` + Tesseract 本体 |
| numpy | 画像データ処理 | `pip install numpy` |

※ `.venv/` に整備済み（実行: `.venv\Scripts\python.exe run_pc_flow.py`）

### 6.3 Pico ハードウェア依存

- `pico_mouse.py` のみが pyserial に依存
- `PicoMouse` のインポートは `try/except ImportError` でラップされており、**Pico 未接続でも GUI は起動する**
- tap/swipe ステップは Pico 未接続時にスキップ（ログに記録）

---

## 7. 起動方法

### 開発時

```bat
cd D:\github2\nightcrows\pc
..\venv\Scripts\python.exe run_pc_flow.py
```

またはプロジェクトルートの `run_pc.bat`（`run_exp_meter.py` 向けのため、別途 `run_pc_flow.bat` を作成するとよい）。

### exe ビルド（PyInstaller）

```bat
cd D:\github2\nightcrows\pc
..\venv\Scripts\pyinstaller --onefile --windowed --name "PCフロー制御" run_pc_flow.py
```

`_ensure_cwd()` が `sys.frozen` フラグを見て `sys.executable` のディレクトリを CWD にセットするため、`scenes/`・`flows/` を exe と同じフォルダに置けば動作する。

---

## 8. 未完了タスク

| # | 内容 | ファイル |
|---|------|---------|
| 5 | 座標記録ツール（`move_test.py` オプション9追加） | `pc/move_test.py` |
| 6 | PC 用シーン JSON の作成 | `pc/scenes/*.json` |
| 6 | PC 用テンプレート画像の収集 | `pc/templates/*.png` |
| - | run_pc_flow.bat の作成（省力化） | プロジェクトルート |

---

## 9. mobile 版との対応表

| 項目 | mobile 版 | PC 版 |
|------|-----------|-------|
| 操作手段 | ADB `input tap / swipe` | Pico HID マウス |
| 座標指定 | デバイス絶対 px | ウィンドウ相対比率（0.0〜1.0） |
| 画面キャプチャ | ADB screencap | Win32 PrintWindow |
| ウォッチャー | あり（常時監視スレッド） | なし（現時点） |
| メインシーケンス | あり | なし（スケジュールのみ） |
| フロー JSON | `mobile/flows/` | `pc/flows/`（同フォーマット） |
| シーン JSON | `mobile/scenes/` | `pc/scenes/`（相対座標に変更） |
| 実行ファイル | `mobile/run_gui.py` | `pc/run_pc_flow.py` |
