# フロー設計

シーン（動作単位）をどう繋ぎ合わせて走らせるかを定義する上位レイヤー。2026-04-23 に導入。実装は `gui/flow.py`、再生エンジンは未実装。

## 2層構造

```
Flow  (flows/*.json)          複数シーンの繋ぎ方を定義
 ├─ main_sequence: [...]        通常再生するシーンの並び
 ├─ schedule:      [...]        時刻で別シーンに切替
 └─ watchers:      [...]        常時監視、条件でハンドラシーンに割り込み
 
Scene (scenes/*.json)         1つの動作単位（タップ/スワイプ/待ち/画像マッチ）
```

- シーンは既存の概念そのまま
- フローは「朝はこのシーン、12:55 になったらこっち、ポーションが切れたらこの割り込みハンドラ」を別ファイルに分離
- メインシーケンスが最後まで行ったら、最後のシーンを繰り返し続ける（`after_main: "stay"` がデフォ）。日付跨ぎは特別扱いしない（必要なら `schedule` に `time: "00:00"` で先頭ジャンプを入れる）

## Flow JSON スキーマ

```json
{
  "name": "daily",
  "version": 1,
  "device_ip": "192.168.0.119",
  "main_sequence": [
    "main/login.json",
    "main/hunt.json"
  ],
  "after_main": "stay",
  "schedule": [
    { "time": "12:55", "target": "main/event.json", "repeat": "daily" },
    { "time": "20:00", "target": "main/weekday_event.json",
      "repeat": "weekly", "days": [0, 1, 2, 3, 4] },
    { "time": "09:00", "target": "main/weekend.json",
      "repeat": "weekly", "days": [5, 6] },
    { "date": "2026-05-01", "time": "20:00",
      "target": "main/special.json", "repeat": "once" }
  ],
  "watchers": [ ... ],
  "settings": {
    "polling_interval_s": 1.0
  }
}
```

### フィールド

| キー | 型 | 意味 |
|------|----|------|
| `name` | str | フロー名（人間用） |
| `version` | int | スキーマバージョン。1 固定 |
| `device_ip` | str | 再生対象デバイスの IP（ポートは自動検出） |
| `main_sequence` | str[] | `scenes/` からの相対パスで、順次再生するシーンを並べる |
| `after_main` | enum | `stay` = 最後のシーンを繰り返す（デフォ） / `stop` = フロー停止 |
| `schedule` | entry[] | 時刻トリガの一覧 |
| `watchers` | watcher[] | 常時監視ルールの一覧 |
| `settings.polling_interval_s` | float | 全 watcher の画面ポーリング周期（秒）。デフォ 1.0 |

### ScheduleEntry

| キー | 型 | 意味 |
|------|----|------|
| `time` | "HH:MM" | 発火する時刻 |
| `target` | str | ジャンプ先シーンのパス（`scenes/` からの相対） |
| `repeat` | enum | `daily` = 毎日 / `weekly` = 曜日指定 / `once` = 指定日に1回 |
| `days` | int[] | `repeat: "weekly"` のとき有効。0=月・1=火・2=水・3=木・4=金・5=土・6=日 |
| `date` | "YYYY-MM-DD" | `repeat: "once"` のとき有効 |

**曜日番号（月スタート）**

| 番号 | 曜日 |
|------|------|
| 0 | 月 |
| 1 | 火 |
| 2 | 水 |
| 3 | 木 |
| 4 | 金 |
| 5 | 土 |
| 6 | 日 |

`days` を省略または空リストにすると毎日発火（`daily` と同じ動作）。

## ウォッチャー（常時監視）

通常のシーン再生中も並行してポーリングし、条件を満たすと現在のシーンを中断してハンドラシーンを実行する仕組み。

### 基本フィールド

| キー | 型 | 意味 |
|------|----|------|
| `id` | str | 識別子（自動生成）。ログ表示用 |
| `title` | str | 表示名（必須）。例: "ポーション低下"・"体力ピンチ" |
| `enabled` | bool | 無効化フラグ |
| `priority` | int | 複数が同時発火したら**値の大きい方**が勝つ（最優先 = 大きい値）。同値は配列順 |
| `condition` | object | 発火条件（下記） |
| `handler` | str | 割り込みで実行するシーン（`scenes/` からの相対） |
| `after` | enum | `restart_scene` = 割り込まれたシーンを1ステップ目から（デフォ） / `next_scene` = `main_sequence` の次へ / `noop` = 何もせず待機に戻る / `stop` = フロー停止 |
| `cooldown_s` | float | ハンドラ完了後、この秒数は同じ watcher を再発火させない |
| `interrupt` | enum | `step_end` = 現在のステップが終わってから割り込む（デフォ、安全） / `immediate` = 即中断 |

### 条件タイプ

#### `image_appear`
テンプレ画像が画面上に見つかったら発火。

| キー | 型 | 意味 |
|------|----|------|
| `template` | str | テンプレ画像のパス |
| `region` | [x,y,w,h] | 検索範囲（論理座標）。省略時は全画面 |
| `threshold` | float | マッチ閾値（デフォ 0.85） |

#### `image_gone`
テンプレ画像が N 回連続マッチしなければ発火。単発の誤検知を防ぐため複数回判定する。

| キー | 型 | 意味 |
|------|----|------|
| `template` / `region` / `threshold` | 同上 | |
| `consecutive` | int | 何回連続で外れたら発火させるか（デフォ 3） |

#### `ocr_number`
Tesseract OCR で指定領域の数値を読み取り、閾値比較する。**ポーション残量・HP 検知などに使用**。`digit_threshold` より手軽（事前テンプレート不要）。

| キー | 型 | 意味 |
|------|----|------|
| `region` | [x,y,w,h] | 数字が表示される領域 |
| `ocr_whitelist` | str | 読み取る文字種（デフォ `"0123456789"`） |
| `op` | enum | `<` / `<=` / `>` / `>=` / `==` |
| `value` | int | 比較対象の数値 |
| `consecutive` | int | N 回連続で条件を満たしたときだけ発火（デフォ 1 = 即時。誤検知対策） |

`consecutive` は省略可。省略時は 1（即時発火）として読み込まれ、JSON にも書き出されない。

事前に Tesseract OCR をインストールし、`pip install pytesseract` を実行する必要がある。  
GUI の「ウォッチャー」タブ > ウィザード OCR パネルで「▶ OCRテスト」を使って動作確認を推奨。

#### `digit_threshold`
指定領域を 0〜9 のテンプレ画像で数字認識し、閾値比較する。

| キー | 型 | 意味 |
|------|----|------|
| `region` | [x,y,w,h] | 数字が表示される領域 |
| `digits_dir` | str | `0.png`〜`9.png` が入ったディレクトリ（通常 `templates/digits`） |
| `op` | enum | `<` / `<=` / `>` / `>=` / `==` |
| `value` | int | 比較対象の数値 |
| `consecutive` | int | N 回連続で条件を満たしたときだけ発火（デフォ 1 = 即時） |

`consecutive` は省略可。省略時は 1（即時発火）として読み込まれ、JSON にも書き出されない。

事前に Tesseract OCR をインストールし、`pip install pytesseract` を実行する必要がある。  
GUI の「ウォッチャー」タブ > ウィザード OCR パネルで「▶ OCRテスト」を使って動作確認を推奨。

#### `digit_threshold`
指定領域を 0〜9 のテンプレ画像で数字認識し、閾値比較する。

| キー | 型 | 意味 |
|------|----|------|
| `region` | [x,y,w,h] | 数字が表示される領域 |
| `digits_dir` | str | `0.png`〜`9.png` が入ったディレクトリ |
| `op` | enum | `<` / `<=` / `>` / `>=` / `==` |
| `value` | int | 比較対象の数値 |
| `consecutive` | int | N 回連続で条件を満たしたときだけ発火（デフォ 1 = 即時） |

### なぜ if/else ではなくウォッチャーか

ゲームの実際の挙動は「基本はこれを続ける、ただし例外条件のときだけ別対応」というリスト構造。if/else で木構造を書くよりウォッチャーの平板リストのほうが素直に書ける。

例：「ポーションが無い時だけ補給、あとは狩り続ける」は、watcher 1個（アイコン消失 → 補給ハンドラ）だけで済む。else 枝（狩り続ける）は暗黙的にメインフローが担う。

### ハンドラ実行中の挙動

- ハンドラ実行中は **他の watcher を止める**（誤発火・入れ子実行を防ぐ）
- 発火した watcher 自身の `cooldown_s` は動き続ける（ハンドラ完了直後の再発火防止）
- ハンドラシーンは普通のシーンと同一フォーマット。シーンエディタで編集可能

## フォルダ規約

```
scenes/
  ├─ main/              メインシーケンス用シーン
  └─ handlers/          割り込みハンドラ用シーン
flows/                  フロー定義
templates/
  ├─ snapshots/         シーンエディタが自動保存するスナップ
  ├─ watchers/          監視用テンプレ画像（HP赤バー等）
  └─ digits/            0.png 〜 9.png
```

- `main/` と `handlers/` の区別は **人間の整理のためだけ**。Flow 側は `scenes/` からの相対パスで参照するので、どこに置いても動く
- ハンドラも「シーン編集タブ」で通常のシーンとして作る（専用エディタは無い）

## 設計上の決定事項（合意済み）

| 項目 | 決定 |
|------|------|
| メインが最後まで行ったら | 最後のシーンを繰り返し続ける（`after_main: "stay"`） |
| 日付跨ぎ | 特別扱いしない。0:00 リセットしたければ schedule に入れる |
| 曜日スケジュール | `repeat: "weekly"` + `days: [0〜6]` で指定。0=月スタート（Python `weekday()` に準拠） |
| 分岐 (if/else) | シーンステップ内で `if_image` を使う。watcher の平板リストで表現することも可 |
| 同時発火 | priority 高 → 配列先 の順で1つだけ実行 |
| ハンドラ終了後 | デフォ `restart_scene`（個別に `next_scene` も可） |
| 割り込みタイミング | デフォ `step_end`（スワイプ途中切りを避ける。個別に `immediate` も可） |
| ポーション残量 | OCR（ゲーム内に警告画像がないため） |
| HP 低下 | HP メーターの画像マッチ（赤バー等） |
| ハンドラ中の他 watcher | 停止 |
| 画像消失の発火判定 | N 回連続（デフォ 3）で誤検知防止 |
| ウォッチャーの polling 間隔 | デフォ 1.0 秒（`settings.polling_interval_s` で変更） |
| ウォッチャー管理 | `watchers/` ディレクトリに個別 JSON で保存（旧: `watchers.json` 1ファイル） |
| restart_scene のフォールバック | まだシーンが動いていない場合は `_last_due_scenes()` で直近スケジュールを推定して実行 |

## ScheduleEntry の `sequence` フィールド

複数シーンをまとめてスケジュール発火させたい場合に使う。

```json
{
  "time": "15:00",
  "sequence": ["scenes/hunt_a.json", "scenes/hunt_b.json"],
  "repeat": "daily"
}
```

`sequence` がある場合は `target` を無視する。`sequence` が空または省略の場合は `target` を1件のリストとして扱う。

## `restart_scene` のフォールバック動作

フローを途中時間から開始すると、過去のスケジュールが全スキップされて `last_running_scene = None` になる。
この状態でウォッチャーが `after=restart_scene` で発火すると「行き先なし」になっていた。

**`_last_due_scenes(flow, now)` で解決（`flow_runner.py`）:**

1. `flow.schedule` を走査し、`now` より前で今日発火すべきだったエントリを抽出
2. 最も時刻が遅いエントリのシーンリストを返す
3. ウォッチャーハンドラ完了後、`last_running_scene is None` の場合にこれを使ってシーンを実行

```
例: 17:00 起動、スケジュール = 9:00 / 12:00 / 15:00
→ 全スキップ、last_running_scene = None
→ 18:21 にウォッチャー発火、after=restart_scene
→ _last_due_scenes が 15:00 エントリを返す
→ 15:00 のシーン（激戦地2 80LV.json）を実行
```

## 実装状況

- [x] データモデル（`gui/flow.py`、`gui/scene.py`）
- [x] フォルダ構成
- [x] GUI タブ化（シーン編集タブはフル機能）
- [x] 再生エンジン（`gui/flow_runner.py`）— main_sequence 順次、`after_main`, step_end 割り込み
- [x] スケジュールトリガ（時刻で別シーンにジャンプ）
- [x] ウォッチャー — `image_appear` + 優先度 + クールダウン + ハンドラ中の pause
- [x] ウォッチャー — `image_gone`（N回連続外れ判定）
- [x] ウォッチャー — `digit_threshold`（0.png〜9.png でテンプレマッチして数値比較）
- [x] ウォッチャー — `ocr_number`（Tesseract OCR で数値読み取り・閾値比較・連続N回判定）
- [x] ウォッチャー編集タブ（スクショベースの1画面ウィザード、`watchers/` ディレクトリで独立管理）
- [x] OCR テストダイアログ（スクショ → ドラッグ範囲選択 → 数値読み取り確認）
- [x] ランナータブ（フロー選択・開始/停止・ログ・即時シーン実行）
- [x] フロー編集タブ（TV番組表スタイルグリッド・1分単位時刻・右クリック即時実行・現在時刻赤線・自動追従）
- [x] `pick_scene` ステップ（ランダム / 順番ローテーション）
- [x] `restart_scene` フォールバック（直近スケジュールへの自動推定）
- [x] ウォッチャータグバー（フロー編集画面下部、クリックで有効/無効切替）
- [ ] 割り込みモード `interrupt: "immediate"`（現状は `step_end` のみ）
- [ ] ランナータブ拡張（現在シーン表示、watcher ステータス、手動トリガ）
