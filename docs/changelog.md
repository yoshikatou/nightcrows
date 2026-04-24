# 作業履歴

変更の経緯と決定事項をセッション単位で記録する。

---

## 2026-04-24（夜間セッション）

### OCR 誤検知対策：連続N回検知オプション

**背景:** OCR 数値判定（`ocr_number`）は1回の読み取り結果だけで発火していたため、画面遷移中の一瞬の表示乱れや OCR の読み誤りで誤発火することがあった。

**解決策:** `image_gone` が持つ `consecutive`（連続N回判定）を `ocr_number` / `digit_threshold` にも適用する。

#### `gui/flow.py`

- `_cond_to_dict`: `ocr_number` / `digit_threshold` の `consecutive > 1` のときだけ JSON に書き出す（1 = デフォルト = 即時発火で、保存しない）
- `_cond_from_dict`: デフォルト値を型によって分ける
  - `image_gone` → デフォルト 3（従来通り）
  - `ocr_number` / `digit_threshold` → デフォルト 1（即時発火。既存 JSON に `consecutive` が無い場合の後方互換）

#### `gui/flow_runner.py`（`WatcherState`）

- `_hit_count: dict[str, int]` を追加（`_miss_count` の逆、ヒット回数のカウンタ）
- `_run()` の発火判定:
  - `ocr_number` / `digit_threshold` は条件を満たすたびに `_hit_count` をインクリメント
  - `_hit_count >= consecutive` で初めて `fires` に追加
  - 条件を外れたら `_hit_count` をリセット
  - ログ: `👁 {id} 連続ヒット N/required` / `👁 {id} 条件外れ — カウンタリセット`
- `mark_fired()` でも `_hit_count` をリセット（ハンドラ実行後に確実にクリア）

#### `gui/watcher_editor.py`

- OCR 条件パネルに「連続検知回数」`QSpinBox`（1〜30、デフォルト 1）を追加
- ヒント文: "1=即時発火、2以上=N回連続で条件を満たしたとき発火（誤検知対策）"
- 編集ダイアログで既存ウォッチャーを開いたとき `_prefill()` で値を復元

**ログ出力例（consecutive=3 の場合）:**
```
👁 514a6550 連続ヒット 1/3
👁 514a6550 連続ヒット 2/3
👁 514a6550 条件外れ — カウンタリセット
👁 514a6550 連続ヒット 1/3
👁 514a6550 連続ヒット 2/3
👁 514a6550 連続ヒット 3/3
👁 watcher 発火検知: 514a6550 (priority=900)
```

**設計上の判断:**
- 「同じ画像を複数回 OCR する」案は Tesseract が決定論的なため効果なし
- 「2枚スクショする」案はコスト増の割に連続N回判定と同等のため不採用
- 連続N回判定は既存の `image_gone` 実装と同じ枠組みで実現できるため採用

---

## 2026-04-24（後半セッション）

### シーン編集 UI 強化（if_image・画像ステップ周り）

#### キャンバスクリックで if_image のタップ座標を指定
- 座標スピンボックスによる入力を廃止。スナップショット画像を直接クリックしてタップ位置を追加できるようにした
- `_ClickableImageLabel(QLabel)` を新設。クリック位置を論理座標に変換して `clicked(x, y)` シグナルを emit
- シーン編集キャンバス上でクリック → ポップアップ（「🟢 then に追加 / 🔴 else に追加」）で分岐先を選択

#### テンプレート再設定（🖼 再設定ボタン）
- 既存の `wait_image` / `tap_image` / `if_image` ステップのマッチ領域を再指定できるボタンを追加
- ボタン押下後はキャンバスのドラッグ操作がテンプレート再設定モードになる
- 完了後に自動でモード解除

#### ステップ選択時のキャンバスオーバーレイ
- `wait_image` / `tap_image` / `if_image` 行を選択すると、キャンバスに青い破線矩形でマッチ領域を表示
- `if_image` では then ブランチのタップ位置を緑（✓N）、else を赤（✗N）の円マーカーで表示

#### if_image 分岐編集ダイアログにスナップショット表示
- `_IfImageBranchDialog` の左ペインにスナップショット＋マッチ領域＋タップマーカーを表示
- ステップを追加/削除するたびにリアルタイムでマーカーが更新（`steps_changed Signal` 経由）
- ダイアログ内の画像をクリックしてもタップを追加できる

#### ステップリスト/ボタンの日本語表記統一
- ボタン：`⏱ 待ち`、`🔑 キー`、`↕ スクロール`、`📂 取込`、`┄ グループ`
- ステップ表示：`👆 タップ`、`⏱ 待ち`、`📷 スナップ`、`🕐 画像待ち`、`👆 画像タップ`、`🔀 画像分岐`、`↔ スワイプ`、`↕ スクロール`、`📂 シーン呼出`

### シーン編集 UI 強化（リスト操作）

#### 複数選択・まとめて削除・まとめて移動
- `QListWidget.ExtendedSelection` に切り替え。Shift クリックで連続選択、Ctrl クリックで個別追加選択
- 削除ボタン：選択行をすべて一括削除（降順インデックスで pop して安全に処理）
- ↑/↓ ボタン：連続ブロックは境界スワップ（ブロック全体をずらす）、非連続は各行を個別移動

### シーン再生中のステップハイライト
- 実行中のステップ行の背景色を `#FFF8E1`（淡い黄）で強調表示
- スレッド境界は `step_highlight_signal = Signal(int)` 経由で安全にメインスレッドに通知
- `replay_scene` に `on_step: Callable[[int], None]` コールバックを追加（depth=0 のみ通知）
- 再生完了時に `_clear_step_highlight()` でハイライト解除

### フロー編集タブ強化

#### 右クリックで即時実行
- グリッドセルの右クリックメニューに「▶ 今すぐ実行」を追加
- ランナータブに切り替えて `runner_widget.run_scenes_now(scenes)` を呼び出す
- `run_scenes_now` はフロー実行とは独立したスレッドで対象シーンを順次実行

#### 現在時刻の赤横線オーバーレイ
- `_TimeLineOverlay(QWidget)` を `_ScheduleTable` のビューポート上に重ね描き
- 現在時刻に対応する行を `visualRect(model().index(row, 0))` で取得し、分単位の端数で行内 y 座標を補間
- `rowViewportPosition` ではスクロール位置がズレる問題を確認。`visualRect` に切り替えて解決
- 30秒タイマーで自動更新、ペン幅 2px、左端に赤丸マーカー付き

#### 現在時刻への自動追従
- 「現在時刻に自動追従」チェックボックスを追加（デフォルト ON）
- ON 時：30秒ごとのタイマー更新で `scrollTo(row, PositionAtCenter)` を呼び出し
- 「今すぐ移動」ボタンでモードに関係なく即座にジャンプ
- 起動 200ms 後に現在時刻へ自動スクロール（初回描画後に確実に動作させるため遅延）

### ウォッチャー UI 強化

#### ウォッチャーリストのタイトル太字化
- `WatcherEditorWidget._make_item` で `QFont.setBold(True)` を設定

#### フロー編集画面下部のウォッチャータグバー
- フローグリッドの下にスクロール可能なタグ列を追加
- 有効ウォッチャー：青タグ（`#1565c0` 背景・白文字・太字）
- 無効ウォッチャー：グレータグ（薄文字）
- タグをクリックして有効/無効をその場でトグル → ファイルに即保存
- `watchers_changed Signal` を `WatcherEditorWidget` に追加し、`main.py` で `flow_editor.refresh_watcher_tags` に接続
- OCR 数値条件のウォッチャーはタグにしきい値を表示（例: `ポーション低下  ≤2300`）

### pick_scene ステップ（パターン選択）

新ステップ型 `pick_scene` を追加。シーンリストから1つを選んで実行する。

| モード | 動作 |
|--------|------|
| `random` | 毎回ランダムに1つを選ぶ |
| `sequential` | 1回目→A、2回目→B…と順番に選び、最後まで来たら先頭に戻る |

**JSON 形式:**
```json
{
  "type": "pick_scene",
  "mode": "sequential",
  "scenes": ["scenes/map_a.json", "scenes/map_b.json"],
  "step_id": "abc12345"
}
```

- `step_id` は作成時に自動生成する 8文字 UUID（フロー内の複数 pick_scene を区別するため）
- `sequential` モードのカウンタは `_seq_state: dict[str, int]` としてフロー実行全体で共有
- `replay_scene` に `_seq_state` 引数を追加し、全サブシーン呼び出し（`call_scene` / `if_image` / `pick_scene`）に伝播
- フロー実行（`replay_flow`）は `seq_state = {}` を生成して全 `run_scene` 呼び出しに渡す
- 停止→再開でカウンタはリセット（フロー開始時に新しい辞書を作るため）

**UI 操作:**
- シーン編集の「🎲 選択」ボタンで追加
- ダブルクリックで `_PickSceneDialog` を開き、モード変更・シーン追加/削除/並替が可能
- ステップ表示例: `3. 🔄 順番選択 3択  [マップA、マップB、マップC]`

### restart_scene フォールバック改善

**問題:** フロー実行を途中から開始した場合（例: 17:00 起動、スケジュールは全スキップ）、ウォッチャーの `after=restart_scene` が `last_running_scene = None` のため無効になっていた。

**修正:** `_last_due_scenes(flow, now)` ヘルパー関数を追加。

- 現在時刻より前で最後に発火すべきだったスケジュールエントリを探す
- `last_running_scene is None` かつ `schedule_only` の場合にこれをフォールバックとして使用
- 見つかったシーン列を順番に実行し、`last_running_scene` も更新する

```
例: 17:00 起動 → 18:21 にウォッチャー発火
→ 18:21 より前の最新エントリ = 15:00 のスケジュール
→ そのシーン（激戦地2 80LV.json）を実行
ログ: → restart_scene: 未実行のため直近スケジュール [激戦地2 80LV.json] を実行
```

---

## 2026-04-23（続き）

### ウォッチャータブの新設

- `gui/watcher_editor.py` を新規作成。「ウォッチャー」タブをフロー編集とランナーの間に追加
- フロー JSON とは独立した `watchers.json`（プロジェクトルート）で管理
  - どのフローを実行中でも共通で適用されるグローバルウォッチャー
  - `gui/flow.py` に `save_watchers()` / `load_watchers()` を追加
  - `runner_widget.py` 起動時にグローバルウォッチャー + フロー内ウォッチャーを合算
- ウォッチャー一覧：追加・編集・削除・上下移動・有効/無効トグル・保存

### ウォッチャー作成 UI のスクショベース化

- 新規作成・編集をウィザード形式（2ページ）に刷新
  - **ページ①**: タイトル入力 + スクショ取得（デバイス or ファイル）+ 範囲ドラッグ選択
  - **ページ②**: 検知方法ラジオボタン選択 + 条件詳細 + アクション設定
- 画像系条件（`image_appear` / `image_gone`）はドラッグ選択した切り抜きをそのまま `templates/` に自動保存
- OCR 条件（`ocr_number`）はページ②でその場でテスト実行して数値読み取りを確認可能
- 編集時は既存テンプレート画像をキャンバスに自動表示

### OCR テスト機能（`gui/ocr_test_dialog.py`）

- スクショ or ファイルを表示し、マウスドラッグで範囲を選択
- Tesseract OCR（`pytesseract`）で数値を読み取りテスト。文字種ホワイトリスト対応
- 切り抜きプレビュー表示。「この範囲をウォッチャーに設定」で region を返す
- `requirements.txt` に `pytesseract>=0.3` を追加

### OCR 条件型（`ocr_number`）の追加

- `gui/flow.py`: `Condition` に `ocr_number` 型と `ocr_whitelist` フィールドを追加
- `gui/flow_runner.py`: `_ocr_number()` 評価関数を追加（Tesseract で region 内の数値を読む）
- 実行時前処理：グレースケール化 → 3倍拡大 → Otsu 二値化でゲームUIの細い数字に対応

### ウォッチャーデータモデルの変更

- `Watcher` に `title` フィールドを追加（必須）。例: "ポーション低下"・"体力ピンチ"
- 未入力で OK を押した場合は警告ダイアログを表示してキャンセル
- 一覧表示・削除確認ダイアログにタイトルを表示
- `id` は内部管理用として自動生成（ユーザーが触る必要なし）

### フロー時刻精度の改善（フロー編集タブ）

- `_ScheduleEntryDialog` を追加。グリッドの30分軸はそのままに、`QTimeEdit` で1分単位の時刻指定が可能に
- セルに `Qt.UserRole+1` で正確な時刻を保存し、表示・保存・再読込に反映

---

## 2026-04-23

### 開発環境セットアップ

- Python 3.10 環境に `.venv/` を作成し、`requirements.txt` 依存をインストール
- `numpy>=2.4` が Python 3.10 では存在しないため `numpy>=1.26` に緩和（numpy 2.2.6 が入る）

### シーンの親子構造（call_scene ステップ）

- `call_scene` ステップ型を追加。子シーンから親シーン（共通処理）を呼び出せる
  - JSON: `{"type": "call_scene", "scene": "scenes/main/open_menu.json"}`
  - 再帰深度 10 で循環参照を防止
- `replay.py`: `call_scene` を再帰的に `replay_scene` で実行する `_do_call_scene` を追加
- `scene_editor.py`: 「サブシーン追加」ボタンでファイル選択 → ステップ末尾に追加
  - ステップリスト表示: `→ open_menu  [scenes/main/open_menu.json]`

**使い方イメージ:**
- `open_menu.json` (親): メニューを開く共通手順
- `go_to_dungeon.json` (子): ステップ1 = `call_scene: open_menu.json`、以降ダンジョン移動手順
- `open_bag.json` (子): ステップ1 = `call_scene: open_menu.json`、以降バッグ操作手順

### フロー編集タブ：TV番組表スタイルの週間スケジュールエディタ

- `gui/flow_editor.py` を新規作成。「フロー編集タブ」のプレースホルダーを置き換え
- 列 = 曜日（月〜日）、行 = 時刻（00:00〜23:30、30分刻み）のグリッドを表示
- セルをクリック → `_ScenePickerDialog` でシーン選択（絞り込み検索付き）
- 右クリック → クリア
- セルはシーンパスのハッシュで色分け、ツールチップにフルパスを表示
- 「開く」「新規」「保存」でフロー JSON を管理。`weekly` エントリをグリッドと相互変換
- `daily` エントリ読込時は全曜日に表示（保存時は `weekly` に変換）
- `once` エントリは JSON 保持のみ（グリッド非表示）

### 日時・曜日表示 / メンテナンス日程登録

- `main.py`: ヘッダーバーに日時・曜日をリアルタイム表示（`QTimer` 毎秒更新）
  - 表示形式: `2026-04-23（水）14:35:22`
- `main.py`: 「🔧 メンテ」ボタンからメンテナンス管理ダイアログを開く
- `gui/maintenance.py`: `MaintenanceEntry(id, label, start, end)` データモデル + `maintenance.json` への保存
- `gui/maintenance_dialog.py`: 一覧表示・追加・編集・削除ダイアログ。実施中エントリは赤字で表示
- `flow_runner.py`: メインループ先頭と `scene_interrupt` でメンテ窓チェック。メンテ中は 30 秒ごとに残り時間をログ出力しながら待機、終了後自動再開
- `runner_widget.py`: フロー開始時に `maintenance.json` を読み込んで `replay_flow` に渡す

### スケジュール：曜日指定対応

- `ScheduleEntry` に `repeat: "weekly"` と `days: list[int]` を追加（0=月〜6=日）
- `flow_runner._check_schedule` に曜日フィルタを追加。`today_weekday not in entry.days` の場合はスキップ
- `days` 省略または空リストの場合は毎日発火（`daily` と同じ動作）
- `flow_design.md` のスキーマ・決定事項・サンプル JSON を更新

### GUI 編集機能の強化

#### キャンバス：マーカードラッグ移動

- タップマーカー（赤い番号円）の上にカーソルを乗せると十字矢印カーソルに変化
- そのままドラッグすると緑色のプレビューが表示され、離した位置にタップ座標を更新
- `canvas.py` に `marker_moved = Signal(int, int, int)` を追加
- `scene_editor.py` の `_compute_view` がマーカーインデックス → ステップインデックスの対応表（`_marker_step_indices`）を返すように変更
- `_on_marker_moved` ハンドラでステップの `x`, `y` を更新

#### キャンバス：右クリックメニューでタップ追加

- キャンバス上を右クリックすると「タップ追加 (x, y)」メニューを表示
- 選択するとその座標にタップステップを末尾追加
- `canvas.py` に `right_clicked = Signal(int, int)` を追加
- タップ追加ロジックを `_add_tap_step(x, y)` に共通化（左クリック・右クリックメニュー両方から呼ぶ）

#### ステップリスト：↑↓ボタンによる並び替え

- ステップリスト下部に「↑ 上へ」「↓ 下へ」ボタンを追加
- 選択行を1ステップずつ移動する

#### ステップリスト：ドラッグ＆ドロップによる並び替え

- `QListWidget.setDragDropMode(InternalMove)` で行のドラッグ移動を有効化
- `model().rowsMoved` シグナルで `scene.steps` をビューの順序に同期

#### 接続状態のボタン色表示

- `main.py` の `_set_connected` / `_adb_connect` で接続状態に応じてボタン色を変更
  - 未接続：接続ボタン = 赤
  - 接続試行中：接続ボタン = オレンジ（無効）
  - 接続中：接続ボタン = 緑、切断ボタン = 赤
