# GUI アーキテクチャ

PySide6 製のシーンエディタ兼ランナー。`gui/` 配下 + `run_gui.py` で起動。

## 起動

```
.venv/Scripts/python.exe run_gui.py
```

依存は `requirements.txt`（PySide6 / opencv-python / numpy）。`.venv/` はプロジェクト直下に置く（gitignore 対象）。

## ウィンドウ構成（タブ）

```
┌──────────────────────────────────────────────────────────────────┐
│ デバイス: [ オフィス ▼ ]  [接続] [切断] [scrcpy起動] [🔧メンテ] [⚙] │
│ 2026-04-23（木）14:35:22                                          │
│ ✓ 接続中: 192.168.0.119:35103                                    │
├─ シーン編集 ─ フロー編集 ─ ウォッチャー ─ ランナー ──────────── │
│                                                                    │
│   ( タブごとに切り替わる )                                          │
│                                                                    │
└──────────────────────────────────────────────────────────────────┘
```

| ファイル | クラス | 役割 |
|---------|--------|------|
| `gui/main.py` | `MainWindow` | 接続バー + QTabWidget のシェル。`current_serial` 保持 |
| `gui/scene_editor.py` | `SceneEditorWidget` | シーン編集タブ。キャンバス + ステップ列 + 記録/再生 |
| `gui/flow_editor.py` | `FlowEditorWidget` | フロー編集タブ。TV番組表スタイルの週間スケジュールグリッド |
| `gui/watcher_editor.py` | `WatcherEditorWidget` | ウォッチャータブ。`watchers.json` をスクショベースで編集 |
| `gui/runner_widget.py` | `RunnerWidget` | ランナータブ。フロー選択・開始/停止・ログ |
| `gui/maintenance_dialog.py` | `MaintenanceDialog` | メンテナンス日程管理ダイアログ（「🔧メンテ」から開く） |

各タブは `MainWindow` への参照を持ち、`current_serial` / `current_ip()` / `set_connected()` / `select_device_by_ip()` で接続状態を参照・更新する。

## プレビュー方針

**プレビューは scrcpy 本体（別ウィンドウ）、GUI 内はスナップショットの静止画のみ。**

- ライブ映像を GUI に埋め込む案は却下。WiFi ADB の `screencap` は 1〜3 fps しか出ず、映像として使えない
- scrcpy の H.264 ストリームを PyAV でデコードする案は実装が重く、依存も FFmpeg 同梱で肥大化する
- 採用: 映像は scrcpy 別ウィンドウに任せ、GUI では1枚ずつスナップショットを取って静止画上で操作する
- 同じ `screencap` 経路が `wait_image` の画像マッチングにも使えるので配管が1本で済む

## スナップショット方針（完全手動）

**画面遷移のたびにユーザーが「スナップ更新」を押す。自動取得しない。**

- 記録開始時の自動スナップは一度実装したが、途中は手動でしか撮れず一貫性が崩れるため削除した
- `snapshot` ステップが挿入され、以降のタップマーカーは新スナップ上に乗る
- スナップ無しで記録開始すると確認ダイアログ
- **Why:** 半自動は判断が分岐して混乱する。ユーザーが明示的に「ここで画面が変わった」と指示する方が後で辿りやすい

## タッチ座標の物理/論理変換

WiFi ADB のタッチ関連 API は2系統に分かれていて、座標空間が違うので変換が必要。

| API | 座標空間 |
|-----|---------|
| `adb shell getevent`（記録側） | **物理座標**（ポートレート基準の 1220×2712） |
| `adb shell input swipe`（再生側） | **論理座標**（現在の画面回転に応じた、ROTATION_270 なら 2712×1220） |

両者が不一致だと「記録した座標と実際のタップ位置がズレる」問題が起きる（2026-04-17 に実機で発覚）。

対応：記録側（`tap_record.py` と `gui/recorder.py`）で `dumpsys input` の Viewport から orientation を取得し、論理座標に変換してから保存する。

変換式：

| 回転 | 式 |
|------|-----|
| 0   | `(x_p, y_p)` |
| 90  | `(y_p, W-x_p)` |
| 180 | `(W-x_p, H-y_p)` |
| 270 | `(H-y_p, x_p)`（実機検証済みはこれのみ） |

**Apply rule:** `getevent` から座標を取る処理を書くときは必ず回転変換を挟む。省略すると座標が合わない。

## Windows の Ctrl+C 対策（getevent ストリーム読み取り）

`for line in proc.stdout:` をメインスレッドで回している最中に Ctrl+C が来ると、`KeyboardInterrupt` が想定外の場所（cleanup の `signal.signal()` 呼び出し中など）で発生して記録が保存されずに終了することがある。

**対応（`tap_record.py`）:** 起動直後に「例外を投げない」 SIGINT ハンドラを立て、`stop_event` をセットして `proc.terminate()` だけ呼ぶ。読み取りは別スレッドで queue に流し、メインは `queue.get(timeout=0.2)` でポーリング。

**Apply rule:** `getevent` 等のストリーム型 subprocess を CLI スクリプトで扱うときはこのパターンを踏襲する。

## 接続フロー（IP だけで繋ぐ）

`gui/adb.py` の `discover_and_connect(ip)` が以下の戦略でポートを自動検出する：

1. 既存の `adb devices` に生きている `IP:*` があればそれを再利用
2. `adb mdns services` でワイヤレスデバッグを広告しているポートを探して接続
3. ポートスキャン（30000〜65535、並列 500、各 0.3s timeout）で開いているポートを列挙し、`adb connect` で順に検証。優先度は 30000-45000 > 45001-55000 > その他

失敗した場合はユーザーに「✗ どの方法でも接続できませんでした」を返す。

設定ファイル `settings.json`（gitignore 対象）は `{"devices": [{"label": "オフィス", "ip": "192.168.255.57"}, ...]}` の形。旧 `"serial": "IP:PORT"` 形式も互換読み込みで IP だけ取り出す（`gui/settings.py: _parse_device`）。

## 切断の検知

scrcpy 停止時に adb 接続が巻き添えで死ぬケースがあるため、`_toggle_scrcpy` で scrcpy を止めた後 `adb_ping` を叩き、応答がなければ「未接続扱い」に落とす（再接続はユーザーが「接続」ボタンを押す）。

## キャンバスのインタラクション

| 操作 | 動作 |
|------|------|
| 左クリック | クリック座標にタップステップを末尾追加 |
| 左ドラッグ（10px 以上） | 矩形選択 → `wait_image` ステップ追加 |
| マーカー上でドラッグ | タップ座標を移動（緑プレビューを表示）|
| 右クリック | コンテキストメニュー → 「タップ追加 (x, y)」 |

マーカーのヒット判定は widget 座標で半径 20px。ホバー時にカーソルを `SizeAllCursor` に変えてドラッグ可能なことを示す。

ドラッグ移動の座標更新フロー：
1. `canvas.marker_moved(marker_idx, lx, ly)` を emit
2. `scene_editor._marker_step_indices[marker_idx]` で `scene.steps` のインデックスに変換
3. `step.params["x"]`, `step.params["y"]` を更新

`_marker_step_indices` は `_refresh_canvas_view` のたびに `_compute_view` が再計算する。

## ステップリストの操作

- 行を選択してスナップ切替・マーカー強調
- 「↑ 上へ」「↓ 下へ」ボタンで 1 ステップずつ移動
- 行をドラッグ＆ドロップで任意位置に移動（`InternalMove` モード）
  - ドロップ後 `model().rowsMoved` シグナルで `scene.steps` をビュー順に同期

## ウォッチャータブ

グローバルウォッチャーを `watchers.json`（プロジェクトルート）で独立管理する。フローを開く必要なく編集可能。

### 新規作成ウィザード（2ページ）

**ページ①: キャプチャ & 範囲選択**
1. タイトル入力（必須。例: "ポーション低下"）
2. スクショ取得（接続デバイス or ファイル）
3. 監視したい箇所をドラッグで選択（ラバーバンド表示）
4. 「次へ →」（タイトル + 範囲が揃うと有効化）

**ページ②: 検知方法 & アクション**
- 切り抜き画像を大きく表示
- 検知方法をラジオボタンで選択：
  - 📷 **画像が出現したとき** (`image_appear`) — 選択範囲をそのままテンプレートに自動保存
  - 📷 **画像が消えたとき** (`image_gone`) — N回連続ミスで発火
  - 🔢 **数値で判定（OCR）** (`ocr_number`) — その場でOCRテスト実行可能
- ハンドラシーン・発火後アクション（再開/次へ/停止）・クールダウン・優先度を設定

### グローバルウォッチャーの適用

ランナー起動時に `watcher_editor.get_watchers()` で読み込み、フロー内ウォッチャーと合算して `replay_flow` に渡す。

```python
# runner_widget.py
global_watchers = self._mw.watcher_editor.get_watchers()
flow.watchers = global_watchers + flow.watchers
```

### 優先度

- 値が**大きいほど優先**される（`-w.priority` 降順ソート）
- 複数のウォッチャーが同時発火した場合、最高優先度の1件のみ実行

## フロー編集タブ

TV番組表スタイルの週間スケジュールグリッド。

- 列 = 曜日（月〜日）、行 = 時刻（00:00〜23:30、30分刻み）
- セルクリック → `_ScheduleEntryDialog` で1分単位の時刻 + シーンを設定
- グリッドの行は30分軸固定、内部の `exact_time` で1分精度を保持（`Qt.UserRole+1`）
- 右クリック → クリア
- セルはシーンパスのハッシュで色分け、ツールチップにフルパス表示
- `daily` エントリ読込時は全曜日に表示（保存時は `weekly` に変換）
- `once` エントリは JSON 保持のみ（グリッド非表示）

## OCR テスト（`gui/ocr_test_dialog.py`）

- スクショ or ファイルを `ImageCanvas` で表示、ドラッグで範囲選択
- Tesseract OCR（`pytesseract`）で数値読み取りテスト
- 前処理: グレースケール → 3倍拡大 → Otsu 二値化（ゲームUIの細い数字向け）
- 文字種ホワイトリスト対応（`tessedit_char_whitelist`）
- 「この範囲をウォッチャーに設定」で `[x, y, w, h]` を返す

## メンテナンス管理（`gui/maintenance.py`）

- `MaintenanceEntry(id, label, start, end)` — 開始・終了日時をもつデータクラス
- `is_in_maintenance(entries, now)` — 現在メンテ中かを返す
- `maintenance.json` に保存、`MaintenanceDialog`（「🔧メンテ」ボタン）で編集
- ランナー: メインループ先頭でチェック。メンテ中は30秒ごとに残り時間をログ出力して待機、終了後自動再開

## シーン/フロー JSON

- シーンの詳細（ステップタイプ、JSON 構造）は `gui/scene.py` を参照
- フローの詳細は `docs/flow_design.md` を参照

## 関連ドキュメント

- [フロー設計](flow_design.md)
- [2拠点開発環境](dev_environment.md)
- [タッチ入力の実装](touch_input.md)
