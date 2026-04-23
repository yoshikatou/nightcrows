# 作業履歴

変更の経緯と決定事項をセッション単位で記録する。

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
