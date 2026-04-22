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
| `repeat` | enum | `daily` = 毎日この時刻 / `once` = `date` の指定日に1回だけ |
| `date` | "YYYY-MM-DD" | `repeat: "once"` のとき有効 |

## ウォッチャー（常時監視）

通常のシーン再生中も並行してポーリングし、条件を満たすと現在のシーンを中断してハンドラシーンを実行する仕組み。

### 基本フィールド

| キー | 型 | 意味 |
|------|----|------|
| `id` | str | 識別子。ログ表示用 |
| `enabled` | bool | 無効化フラグ |
| `priority` | int | 複数が同時発火したら値の大きい方が勝つ。同値は配列順 |
| `condition` | object | 発火条件（下記） |
| `handler` | str | 割り込みで実行するシーン（`scenes/` からの相対） |
| `after` | enum | `restart_scene` = 割り込まれたシーンを1ステップ目から（デフォ） / `next_scene` = `main_sequence` の次へ / `stop` = フロー停止 |
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

#### `digit_threshold`
指定領域を 0〜9 のテンプレ画像で数字認識し、閾値比較する。**ポーション残量検知用**。

| キー | 型 | 意味 |
|------|----|------|
| `region` | [x,y,w,h] | 数字が表示される領域 |
| `digits_dir` | str | `0.png`〜`9.png` が入ったディレクトリ（通常 `templates/digits`） |
| `op` | enum | `<` / `<=` / `>` / `>=` / `==` |
| `value` | int | 比較対象の数値 |

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
| 分岐 (if/else) | 入れない。watcher の平板リストで表現 |
| 同時発火 | priority 高 → 配列先 の順で1つだけ実行 |
| ハンドラ終了後 | デフォ `restart_scene`（個別に `next_scene` も可） |
| 割り込みタイミング | デフォ `step_end`（スワイプ途中切りを避ける。個別に `immediate` も可） |
| ポーション残量 | OCR（ゲーム内に警告画像がないため） |
| HP 低下 | HP メーターの画像マッチ（赤バー等） |
| ハンドラ中の他 watcher | 停止 |
| 画像消失の発火判定 | N 回連続（デフォ 3）で誤検知防止 |
| ウォッチャーの polling 間隔 | デフォ 1.0 秒（`settings.polling_interval_s` で変更） |

## 実装状況

- [x] データモデル（`gui/flow.py`、`gui/scene.py`）
- [x] フォルダ構成
- [x] GUI タブ化（シーン編集タブはフル機能）
- [x] 再生エンジン（`gui/flow_runner.py`）— main_sequence 順次、`after_main`, step_end 割り込み
- [x] スケジュールトリガ（時刻で別シーンにジャンプ）
- [x] ウォッチャー — `image_appear` + 優先度 + クールダウン + ハンドラ中の pause
- [x] ウォッチャー — `image_gone`（N回連続外れ判定）
- [x] ウォッチャー — `digit_threshold`（0.png〜9.png でテンプレマッチして数値比較）
- [x] ランナータブ（最小：フロー選択、開始/停止、ログ）
- [ ] 割り込みモード `interrupt: "immediate"`（現状は `step_end` のみ）
- [ ] フロー編集タブ（JSON 手書き不要化）
- [ ] ランナータブ拡張（現在シーン表示、watcher ステータス、手動トリガ）
- [ ] 領域指定ダイアログ（スナップ上でドラッグして region を決める）
- [ ] ランナー分離ウィンドウ化
