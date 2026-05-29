# PC システム設計書

作成: 2026-05-22
更新: 2026-05-23（ウォッチャー・編集ウィンドウ群・週間スケジュール表示まで反映）
ステータス: **稼働中（シーン作成・運用フェーズ）**

> 旧版（実装初期）は履歴として `docs/pc_flow_design.md` に残してある。本書は現在の実装を反映した上位ドキュメント。

---

## 1. 概要

Night Crows PC 版を Win32 キャプチャ + Pico HID マウス + テンプレマッチで自動操作するシステム。

| 項目 | 内容 |
|------|------|
| 入力 | Raspberry Pi Pico USB HID マウス（`PicoMouse`）+ Win32 `keybd_event`（キー入力のみ） |
| キャプチャ | Win32 `PrintWindow`（`capture_window`） |
| 画像認識 | OpenCV `cv2.matchTemplate(TM_CCOEFF_NORMED)` |
| OCR | Tesseract + `pytesseract`（複数 PSM 試行で精度向上） |
| 操作定義 | JSON（シーン / フロー / ウォッチャー） |
| UI | PySide6（メイン + 編集サブウィンドウ群） |
| 起動 | `run_pc_flow.bat` または `python pc/run_pc_flow.py` |

---

## 2. ファイル構成

```
pc/
├── run_pc_flow.py          # エントリーポイント（CWD を pc/ に固定）
├── pico_mouse.py           # Pico HID マウスコントローラ
├── scenes/                 # シーン JSON
├── flows/                  # フロー JSON
├── watchers/               # ウォッチャー JSON
├── snapshots/              # スクショ保存（.gitignore）
├── templates/<scene>/      # シーン用テンプレ画像
├── watcher_templates/      # ウォッチャー用テンプレ画像
├── logs/                   # 日次ログ（YYYY-MM-DD.log）
├── debug/                  # OCR 入力画像など一時保存（.gitignore）
└── gui/
    ├── pc_main.py          # メインウィンドウ（縦長 1/4）
    ├── pc_scene.py         # シーン実行エンジン
    ├── pc_scene_editor.py  # シーン編集ウィンドウ（独立、大きめ）
    ├── pc_canvas.py        # スナップショット表示キャンバス（ズーム/パン対応）
    ├── pc_watcher.py       # ウォッチャーモデル + 評価
    ├── pc_watcher_editor.py # ウォッチャー編集ウィンドウ（独立）
    ├── pc_flow.py          # フロースケジューラー + ウォッチャー統合
    ├── pc_flow_editor.py   # フロー編集ウィンドウ（週間スケジュール）
    ├── logger.py           # 共通ログ（write_log / purge_old_logs）
    ├── capture.py          # Win32 キャプチャ
    ├── exp_meter.py        # 経験値計測コア（中央値フィルタ等）
    ├── ocr.py              # 前処理 + ocr_digits_best（複数バリアント）
    ├── window_picker.py    # ウィンドウ選択ダイアログ
    ├── region_picker.py    # 経験値メーター用領域ピッカー
    ├── tesseract.py        # Tesseract 検出・パス適用
    ├── settings.py         # settings.json 読み書き
    ├── main.py             # 経験値メーター単体 GUI
    └── overlay.py          # 経験値オーバーレイ
```

---

## 3. メインウィンドウ（縦長 1/4 想定: 約 480×1080）

Windows 11 のスナップで画面 1/4 に収まる縦長レイアウト。常設ヘッダー + タブ。

```
┌─ ヘッダー ──────────────────┐
│  ゲームウィンドウ選択       │
│  Pico マウス接続            │
├─ タブ ─────────────────────┤
│ [実行][テスト][見張り][作成][録画][経験値][翻訳][ログ]
└────────────────────────────┘
```

| タブ | 内容 |
|------|------|
| 実行 | フロー選択・開始/停止・**曜日ページャー (◀ ▶ 今日)** で当日該当エントリのみ表示・次回予定・📅 フロー全体編集ボタン |
| テスト | 移動モード（滑らか / ジャンプ）・速度スライダー・座標 X/Y・現在位置/クリック取得（オーバーレイで複数件）・HID移動/キャリブ・左右クリック・ドラッグ・実行ログ |
| 見張り | ウォッチャー一覧（チェックで即トグル）・新規/編集/削除 |
| 作成 | シーン一覧・新規/編集/複製/削除 |
| 録画 | 対象ウィンドウの mp4 連続録画（fps 可変・1 時間ごと分割）。ウォッチャー発火→ハンドラー完了の自動録画にも同じ WindowRecorder を共用 |
| 経験値 | 独立アプリ (`run_exp_meter.py`) と同じ `ExpMeter` を内蔵し、計測領域 / 桁数ヒント / 計測間隔の設定、現在値・速度・LvUP 予測（絶対時刻併記）表示、🗕 ゲーム画面オーバーレイへの切替（独立アプリと exp_meter.json を共有） |
| 翻訳 | チャット領域を定期キャプチャ → Claude API でベース言語へ翻訳・ユーザー入力を逆翻訳 |
| ログ | フロー実行・スクショ取得・Tesseract 状態などの集約ログ表示 |

「実行」タブと「フロー編集ウィンドウ」は **1 秒ごとの QTimer** で日付跨ぎを検知。「今日」表示が固定されないよう自動追従。

---

## 4. 編集ウィンドウ群（独立した広めの画面）

メイン縦長ウィンドウとは別に開く。複数同時起動可。

### 4.1 シーン編集 `pc_scene_editor.py` (≈ 1000×800)

- 上部: シーン名・対象ウィンドウ・保存
- 左: `PcSnapshotCanvas`（スクショ + マーカー + ズーム/パン）
- 右: 操作ボタン群・速度設定・タップ後待機チェック・ステップ一覧・選択行/再生実行・実行ログ
- 操作:
  - スクショ取得 → `snapshots/snap_*.png` + snapshot ステップ追加
  - キャンバス**クリック** → タップ追加（オプションで待機セット）
  - キャンバス**ドラッグ** → メニュー（画像出現待ち / 画像をタップ / スワイプ / スクロール / 画像で分岐）
  - 「+ その他のステップ ▾」: シーン呼び出し / シーン抽選 / キー入力 / 見出し
  - 「待機 追加」ボタン
- 表示は日本語化済み（JSON 内部は英語タイプ名のまま）
- 保存 / 閉じる時はシグナル `saved` / `closed` を emit してメインの一覧と即同期

### 4.2 ウォッチャー編集 `pc_watcher_editor.py` (≈ 1000×800)

- 上部: タイトル・対象ウィンドウ・保存
- 左: キャンバス（スクショ表示、ドラッグで領域選択）
- 右:
  - スクショ取得
  - 検知タイプ（image_appear / image_gone / ocr_number）
  - 閾値 or OCR 設定（文字種・演算子・値・連続回数）
  - ハンドラーシーン選択（scenes/ から）
  - 完了後動作（noop / restart_scene / next_scene / stop）
  - 優先度 / 冷却 / ポーリング min〜max / 有効 / 通知
- 単発テスト（現在のスクショで判定）・連続監視テスト（バックグラウンド）
- OCR テスト時は `debug/ocr_*.png` に入力画像を保存

### 4.3 フロー編集 `pc_flow_editor.py` (≈ 1100×800)

- 上部: フロー名・保存
- 中央: **7 曜日 × 48 時刻スロット (30 分刻み)** の QTableWidget
  - daily = 青文字（全曜日）/ weekly = 緑文字（days[] のみ）/ once = オレンジ文字（date の曜日）
  - 無効エントリは灰色
  - **今日の曜日列は薄黄背景 + ヘッダー太字**
  - **現在時刻に水平赤線**（1 秒ごとに移動、分秒の小数 row で滑らかに）
- セルダブルクリック → 新規 / 編集ダイアログ
- 編集ダイアログ: 時刻 / シーン選択 / 繰り返し radio (毎日 / 週次 / 1回限り) / 曜日チェック / 日付 / 有効
- 「+ 新規エントリ」「選択を編集」「選択を削除」
- 1 秒ごとに viewport 更新 + 日付跨ぎ検出で曜日着色を全更新

---

## 5. データモデル

### 5.1 シーン (`pc_scene.PcScene`)

```python
@dataclass
class PcScene:
    name: str
    window_title: str
    steps: list[PcStep]

@dataclass
class PcStep:
    type: str
    params: dict
```

JSON 保存: `pc/scenes/<name>.json`

### 5.2 ステップタイプ一覧（実装済み 11 種）

| type | 表示名 | 主要パラメータ |
|------|--------|-----------------|
| `wait_fixed` | 待機 | `seconds` |
| `snapshot` | 画像出現待ち | `path` (PNG) / `threshold` / `timeout_s` |
| `tap` | タップ | `rx, ry` (0.0〜1.0) / `button` / `duration_ms` |
| `tap_image` | 画像をタップ | `template` / `threshold` / `timeout_s` / `region` / `tap_offset_x/y` |
| `swipe` | スワイプ | `rx1, ry1, rx2, ry2` / `duration_ms` |
| `scroll` | スクロール | swipe + `*_jitter` / `duration_jitter_ms` |
| `keyevent` | キー入力 | `key` (esc/enter/f1-f12/a-z 等) / `duration_ms` |
| `group_header` | 見出し | `label` （実行は no-op） |
| `call_scene` | シーン呼び出し | `scene` |
| `if_image` | 画像で分岐 | `template` / `threshold` / `region` / `then_scene` / `else_scene` |
| `pick_scene` | シーン抽選 | `mode` (random/sequential) / `scenes` |

**循環参照ガード:** call_scene / if_image / pick_scene のサブ呼び出しは `_MAX_CALL_DEPTH=10` と `_call_stack` で循環を防ぐ。

### 5.3 ウォッチャー (`pc_watcher.PcWatcher`)

```python
@dataclass
class WatcherCondition:
    type: str                    # image_appear / image_gone / ocr_number
    template: str                # 画像系
    region: list[float] | None   # [rx, ry, rw, rh]
    threshold: float
    ocr_whitelist: str           # OCR
    op: str                      # <, <=, ==, !=, >=, >
    value: float
    consecutive: int             # N 回連続ヒット → 発火

@dataclass
class PcWatcher:
    id: str  (8桁 UUID)
    title: str
    enabled: bool
    priority: int
    condition: WatcherCondition
    handler: str                 # scenes/ 相対
    after: str                   # noop / restart_scene / next_scene / stop
    cooldown_s: float
    alert_desktop: bool
    poll_min_s: float
    poll_max_s: float
```

JSON 保存: `pc/watchers/<title>_<id>.json`
テンプレ保存: `pc/watcher_templates/<id>.png`

### 5.4 フロー (`pc_flow.PcFlow`)

```python
@dataclass
class ScheduleEntry:
    time: str              # "HH:MM"
    target: str            # シーン名 (scenes/ 相対)
    sequence: list[str]    # 追加シーン列（target に続けて実行）
    repeat: str            # daily / weekly / once
    days: list[int]        # 0=月..6=日 (weekly)
    date: str              # YYYY-MM-DD (once)
    enabled: bool

@dataclass
class PcFlow:
    name: str
    version: int
    schedule: list[ScheduleEntry]
    settings: FlowSettings   # polling_interval_s
```

JSON 保存: `pc/flows/<name>.json`

---

## 6. フロー実行ループ

```
PcFlowRunner._run()
├─ 起動時:
│  ・watchers/ から有効なものを読み込み
│  ・別スレッドで watcher_loop を開始
│  ・起動時刻より前の本日エントリは last_fired に登録（重複防止）
│
├─ メインループ (poll = settings.polling_interval_s, 既定 1.0s):
│  ├─ 1) 発火キュー処理（優先度降順）
│  │   ・handler を実行 (watcher_paused.set 中)
│  │   ・after: stop=フロー停止 / restart_scene=シーン再開 /
│  │             next_scene=次へ / noop=何もしない
│  ├─ 2) スケジュール発火確認
│  │   ・check_schedule() → ScheduleEntry
│  │   ・entry_scenes() で target + sequence を解決
│  │   ・_run_scenes_with_watcher() で順次実行（割込対応）
│  └─ 3) 0.1s 単位の待機
│
└─ 終了:
   ・watcher_loop 停止
   ・state_changed("idle")
```

### 6.1 ウォッチャーポーリング (`PcFlowRunner._watcher_loop`)

別スレッドで動き、`poll_min_s`〜`poll_max_s` のランダム間隔で全 watcher を評価。

```
while not _watcher_stop:
    if _watcher_paused: sleep & continue
    img = capture_window(hwnd)
    for w in watchers:
        if cooldown 中: skip
        r = evaluate_watcher(img, w)
        if r.fired:
            _hit_counts[w.id] += 1
            if _hit_counts[w.id] >= w.condition.consecutive:
                _fired_queue.append((w, info))
                _watcher_pending.set()
        else:
            _hit_counts[w.id] = 0
    sleep(random.uniform(pmin, pmax))
```

### 6.2 シーン中断 → 復帰

`run_pc_scene` の `should_stop` は `_stop_event` OR `_watcher_pending` で判定。ウォッチャー発火で True を返してシーンを抜け、メインループが発火キューを処理して `after` に応じて復帰する。

---

## 7. 座標系の運用方針

すべて **ウィンドウクライアント領域の相対比率 (0.0〜1.0)** で保存。実行時に `win32gui.GetClientRect` で現在サイズを取得して絶対座標に変換するため、**ウィンドウ位置移動には完全追従**する。

ただし `cv2.matchTemplate` は固定サイズ画像での比較なので、**ウィンドウサイズ（解像度）が変わるとテンプレマッチが動かなくなる**。

**運用方針:**
- シーン作成時と実行時で Nightcrows ウィンドウのサイズを揃える（A 運用）
- 必要になったら撮影時サイズを JSON に記録してリサイズマッチを実装する（B 案・未着手）

---

## 8. ログとローテーション

`pc/gui/logger.py` の `write_log()` で全コンポーネントが `logs/YYYY-MM-DD.log` に追記する。

| 書き出し元 | 内容 |
|---|---|
| `ExpMeter._log` | 経験値計測の取得・LvUP 等 |
| `PcFlowRunner._log` | フロー開始/停止、スケジュール発火、シーン実行、🔥 ウォッチャー発火、ハンドラー実行、after 動作 |
| `WatcherEditorWindow._append_log` | テスト中の **発火イベント** のみ（評価ごとの行は画面のみ） |

起動時に `purge_old_logs(retain_days=30)` で 30 日より古い `.log` を削除（`settings.log_retain_days` で変更可）。

---

## 9. 入力レイヤー（Pico HID マウス + Win32 キー）

### 9.1 マウス

`PicoMouse`（`pc/pico_mouse.py`）。詳細は `docs/pc_pico_mouse.md` 参照。

PC GUI 側で確定した運用ルール（`docs/pc_pico_mouse.md` に同内容）:

- **クリックはジャンプ**（`SetCursorPos` で位置設定 → Pico HID クリック）
- **ドラッグの開始位置移動はジャンプ可**（運用画面のリモートツールと同じ挙動）
- **ドラッグ本体は必ず滑らか HID 相対移動** (固定 160 px/s + イーズアウト)。`SetCursorPos` だと Raw Input にマウス移動イベントが届かず、ゲームが drag として認識しないため
- 開始位置到達後に **一呼吸 (200ms)** 挟んで `press("L")` → `release("L")`

### 9.2 キーボード

`keyevent` ステップで `ctypes.windll.user32.keybd_event` を呼ぶ Win32 ベース。
キー名→VK コードの対応は `_KEY_VK_MAP` に定義（esc / enter / tab / space / 矢印 / f1-f12 / 単文字）。

> Pico に keyboard 拡張を載せれば物理 HID キー入力にできるが現状未実装。チート対策で SendInput キー入力が弾かれる場合は将来検討。

---

## 10. 既知の制約・運用上の注意

| 項目 | 制約 |
|------|------|
| ウィンドウサイズ | テンプレマッチが固定サイズ依存なので、作成時と実行時を揃える運用 |
| TeamViewer 経由 | カーソル位置取得・Pico 操作・オーバーレイは動く。`InjectTouchInput` はリモートセッション制限で不可（経緯は `docs/pc_input.md`） |
| Pico の必須性 | Nightcrows のチート対策で `SendInput` クリックは弾かれるため、Pico HID 必須 |
| pick_scene の sequential | プロセス内 `_pick_counters` で順番管理。アプリ再起動でリセット |
| マウス加速 | Windows のポインター加速が ON だと `move_to` の精度が落ちる。HID移動のスライダー最遅 (25 px/s) なら影響軽微 |

---

## 11. 関連ドキュメント

- `docs/pc_flow_design.md` — 当初構想（実装前のメモ）
- `docs/pc_pico_mouse.md` — Pico HID マウスの実装詳細・ドラッグ戦略
- `docs/pc_input.md` — PC 自動入力の検証メモ（SendInput / Touch Injection の結論）
- `docs/changelog.md` — 作業履歴（セッションごとの差分）
