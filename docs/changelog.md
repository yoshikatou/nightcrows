# 作業履歴

変更の経緯と決定事項をセッション単位で記録する。

---

## 2026-05-29（短距離 click_at の detour 経由化 + 経験値タブ追加 + 手動スクショの映り込み対策 + 画像ステップの値編集 GUI）

### tap_image / wait_image の値編集ダイアログを追加 (`pc/gui/image_step_dialog.py` 新規, `pc_scene_editor.py`)

**動機:** tap_image で「テンプレ＝region と同サイズ」だとスライド余地ゼロで実質マッチ不可。閾値や探索領域を調整したくても、これまで JSON 直接編集しか手段が無く敷居が高かった。さらに「どの画像をどこで探すか」がダイアログ上で見えず、人間の感覚で対応関係を掴みにくかった。

**修正:**
- `pc/gui/image_step_dialog.py` を新設、`ImageStepEditDialog` を提供
  - **「探したい画像 (テンプレート)」プレビュー**: テンプレ画像と px サイズを表示
  - **検出条件** グループ: 一致閾値 / タイムアウト
  - **「探す場所」(ゲーム画面のクライアント比率)** グループ: X/Y/W/H スピンまたは「領域なし」チェック
  - **テスト** グループ: 「▶ 今のゲーム画面でテスト」ボタン
    - 現在のゲーム画面をキャプチャ → 現在の設定で `matchTemplate` 実行
    - 結果を表示: ✓ 一致 score=0.87 / ✗ 不一致 best score=0.65 / ⚠ 探索領域がテンプレより小さい 等
    - 一致候補位置を **緑(一致)/赤(不一致) の枠** で重ねた領域プレビューを表示
  - **クリック位置オフセット** (tap_image のみ): テンプレ中心からの相対 px
  - **「テンプレを変更…」ボタン**: 現在のゲーム画面をキャプチャ → `RegionPickerDialog` でドラッグして新テンプレ領域を選択 → `templates/<シーン名>/tpl_<ts>.png` に保存して差替え（ダイアログを閉じずに完結）
  - **「範囲を画像で選択…」ボタン**: 同じく `RegionPickerDialog` で探す範囲をドラッグ → X/Y/W/H に自動反映、「領域なし」チェックは自動解除
- scene editor の `_edit_step_params` ハンドラに `tap_image` / `wait_image` を登録
- 呼出側から `window_title` を渡してテスト機能で対象ゲームをキャプチャできるよう拡張
- ステップ選択 → 「⚙ 値編集」ボタンで起動



### 手動スクショ撮影時に自分側ウィンドウを退避 (`pc/gui/capture_clean.py` 新規, `pc_scene_editor.py`, `pc_watcher_editor.py`, `region_picker.py`)

**背景:** 1 画面運用で編集ウィンドウ等が対象ゲームに重なっていると、Nightcrows のような DirectX 系では PrintWindow が空フレームを返し画面 BitBlt フォールバックになり、自分側 GUI が映り込んでしまう。

**修正:**
- `pc/gui/capture_clean.py` を新設、`capture_window_clean(hwnd, settle_s=0.15)` を提供
- 撮影中だけ `QApplication.topLevelWidgets()` の可視 widget 全てを `(-30000, -30000)` へ move、150ms 待ってからキャプチャ、finally で元位置に戻す
- 適用箇所: scene editor / watcher editor の `_capture_snapshot` と `RegionPickerDialog._capture`
- ポーリング系（ウォッチャー監視・経験値 OCR・録画）は毎回ちらつくため対象外。元の `capture_window` をそのまま使う



### フロー側 GUI に経験値計測タブを追加 (`pc/gui/pc_main.py`)

**動機:** 独立した「経験値メーター」アプリ (`run_exp_meter.py` / `gui/main.py`) の機能を、フロー制御アプリ側にも組込んで、別アプリを並行起動しなくても経験値の自動計測ができるようにする。

**実装:**
- 既に import されていた `ExpMeter` を実体化（`self._exp_meter`、`self._exp_overlay`）
- 録画タブの次に「経験値」タブを追加（`_build_tab_exp_meter`）
  - 計測領域（RegionPickerDialog 起動）、桁数ヒント、計測間隔の設定
  - 現在値 / 現在速度 / 平均速度 / LvUP 予測 / 計測時間 表示
  - ▶ 計測開始 / 🔄 リセット / 🗕 ゲーム画面に重ねる
- オーバーレイは独立アプリと同じ `OverlayWindow` を `ExpOverlayWindow` として再利用、位置は `overlay_pos` で永続化
- 対象ウィンドウとTesseract設定はフロー側のメイン設定を共用（個別欄なし）
- 状態は `exp_meter.json` で永続化（独立アプリとデータ共有）
- closeEvent で stop + save + オーバーレイ位置保存



### `click_at` 短距離移動に detour（遠回り）モードを追加 (`pc/pico_mouse.py`)

**背景:** リモート接続無しでも、短距離 (≤80px) のタップだけ視覚的にズレる事象がポーション補給シーン等で観測される。GetCursorPos ベースの誤差は ±2-3px に収まっているのに着弾点が下にズレるため、HID 加速の建ち上がり不足 / 終端の沈み込みが疑われる。

**修正:**
- `click_at` の短距離分岐で、`short_move_detour=True`（既定）なら target から `_SHORT_MOVE_DETOUR_OFFSET_PX=300` 離れた位置へ一旦ジャンプしてから target へ長距離アプローチ
- 画面端で detour 位置が範囲外にならないよう、target 座標が `off` より大きい側からは引き、小さい側からは加える
- 旧挙動（短距離直行モード）は `mouse.short_move_detour = False` で復活
- 1 タップあたり ~500-700ms 増。9 タップ程度のシーンで約 5 秒増の見込み

**検証:** 次回 BF3 94LV シーンや ポーション補給シーンを再生し、短距離タップの着弾点がズレなくなるか確認。改善しなければ revert + 別アプローチ。

---

## 2026-05-28（昨日フォールバック + ウォッチャー発火の自動録画 + if_image インライン手順）

### `if_image` ステップに then/else インライン手順を追加 (`pc/gui/pc_scene.py`, `pc/gui/if_image_dialog.py`, `pc/gui/pc_scene_editor.py`)

**動機:** 「特定の画像が見えたらクリック、無ければ別の処理」のような分岐を、わざわざ別シーンファイルに分けずに 1 つの if_image ステップ内に書きたい要望。

**スキーマ変更:**
```json
{
  "type": "if_image",
  "template": "templates/.../btn.png",
  "threshold": 0.85,
  "region": [...],
  "then": [
    {"type": "tap", "rx": 0.5, "ry": 0.6},
    {"type": "wait_fixed", "seconds": 1.5}
  ],
  "else": []
}
```

- `then` / `else` がリスト（非空）ならインライン手順として再帰実行（`depth+1`、循環参照チェックは _call_stack 経由で継続）
- 空または未指定なら従来の `then_scene` / `else_scene`（シーン名）にフォールバック
- インラインで使えるステップタイプ（ダイアログ経由）: `tap` / `wait_fixed` / `keyevent` / `call_scene`
- それ以外（`tap_image` / `wait_image` / `swipe` 等の位置指定が必要なもの）はキャンバスでの作成が前提のため、現状インラインでは扱わない

**編集 UI:** 新規 `pc/gui/if_image_dialog.py` の `IfImageEditDialog`。
- ステップ一覧で if_image を選択 → 「⚙ 値編集」ボタンでダイアログを開く
- 成立時 / 不成立時 それぞれで「インライン手順 / シーン呼出 / 何もしない」を切替
- インライン手順は QListWidget で追加・編集・並べ替え・削除

**追加エントリポイント:**「+ その他のステップ ▾」メニューに「画像で分岐 追加」を新設。クリックすると:
- 「ドラッグで作る (推奨)」: キャンバスドラッグ手順を案内するメッセージ
- 「既存テンプレ画像を選ぶ」: `templates/` からファイル選択 → そのまま IfImageEditDialog が開く

**ステップラベル:** インライン手順がある場合は「成立→[インライン N 手順]」「成立→<scene_name>」「成立→(なし)」を切替表示

### `_settle_before_click` を revert (`pc/pico_mouse.py`)

TeamViewer 経由でクリック位置がズレる事象の対策として入れた settle ロジックが、ローカル単発でもズレを起こす副作用が出た（短ステップ移動 + GetCursorPos 安定待ちでカーソル微小揺らぎの吸収を仕損ね、補正 move_to が悪化させた可能性）。原因究明を優先するため `_settle_before_click` および関連定数を削除し `click_at` を直前のシンプル実装へ戻した。

### `last_due_scenes` に昨日フォールバックを追加 (`pc/gui/pc_flow.py`)

**背景:** 5/28 03:33 に死亡ウォッチャーが発火したが、当日の最初のスケジュール（daily 06:10 DQ）より前の時刻だったため `last_due_scenes` が `None` を返し、`after=restart_scene` が「直近スケジュールが見つからずスキップ」となった。

**修正:** 候補抽出を `_due_scenes_on(flow, target_date, max_hm_exclusive)` に切り出し、`last_due_scenes` は今日 → 昨日の 2 段階探索に拡張。昨日は曜日 / once 日付も対象日付基準で判定し直す。

### ウォッチャー発火→ハンドラー完了の自動録画 (`pc/gui/pc_flow.py`, `pc/gui/recorder.py`, `pc/gui/pc_main.py`)

**目的:** 発火時の handler シーンがどこまで進んだかを後追い確認するため、発火〜handler 完了の区間を自動録画する。

**実装:**
- `WindowRecorder.start(..., prefix="rec")` でファイル名プレフィックスを差し替え可能に
- `PcFlowRunner.set_recorder(recorder, fps)` で `pc_main` 側の `WindowRecorder` を共有
- `_handle_fired` 内の `_run_scene(handler)` 前後で `_start_auto_record` / `_stop_auto_record` を呼ぶ
- 手動録画中（`is_recording=True`）の発火では自動録画はスキップして既存セッションを尊重
- 録画ファイル名: `rec_watcher_<title>_YYYYMMDD_HHMMSS.mp4`
- 録画タブの FPS スピンボックス変更が自動録画にも反映される

### シーン編集「選択行を実行」を複数選択対応 (`pc/gui/pc_scene_editor.py`)

`_list_steps` を `ExtendedSelection` に変更。Ctrl/Shift クリックで複数行選択 → 「選択行を実行」で昇順連続実行。`_launch_thread` に `rows: list[int]` 引数を追加し、非連続選択でも正しくハイライト。DEL キーも複数行削除に対応。

---

## 2026-05-23（フロー編集ウィンドウ・曜日ページャー・シーン編集の日本語化）

### メイン「実行」タブを曜日フィルタ + ページ送りに改造

**背景:** 縦長 1/4 のメインウィンドウでは全スケジュールを一覧表示すると埋もれるので、今日の曜日に該当するエントリだけ表示し、`◀ ▶` ボタンで前後の曜日を確認できるようにした。

**実装内容（`pc/gui/pc_main.py`）:**
- 「実行」タブに `◀` `▶` `今日` ボタン + 曜日ラベルを追加
- フィルタ仕様: `daily` は全曜日、`weekly` は `days[]` 該当のみ、`once` は `date` の曜日に一致時のみ
- 今日表示時は青文字（「今日 — 金曜日」）、他日は灰色（「土曜日（+1日）」）
- `📅 フロー全体編集…` ボタンで `FlowEditorWindow` を別ウィンドウ起動

### `pc/gui/pc_flow_editor.py` を新規作成（独立ウィンドウ 1100x800）

**役割:** 1 週間を俯瞰できる週間スケジュールテーブル。

- `7 曜日 × 48 時刻スロット (30 分刻み)` の `QTableWidget`
- daily エントリは全曜日に展開（青文字）、weekly は `days[]` に展開（緑文字）、once は `date` の曜日に表示（オレンジ文字）
- 無効エントリは灰色、ホバーで詳細ツールチップ
- セルダブルクリックで新規 / 編集ダイアログ（時刻・シーン選択・繰り返し radio・曜日チェック・日付・有効）
- 「+ 新規エントリ」「選択を編集」「選択を削除」ボタン
- 「保存」で `flows/<name>.json` を上書き、メイン側の一覧と次回予定が即更新

### 現在時刻の赤線 + 今日の曜日列ハイライト

**実装内容:**
- `_ScheduleTable(QTableWidget)` サブクラスを定義し、`paintEvent` で現在時刻に対応する Y 位置に 2px の **赤い水平線** を描画（分・秒を反映、小数 row 位置で滑らかに移動）
- 今日の曜日列の背景を **薄黄色 (`#fff9c4`)** に着色、ヘッダーラベルを太字に
- エントリがない空セルにも背景色を入れて列全体を統一

### 日付跨ぎ対応（起動しっぱなしでも動く）

**実装内容:**
- フロー編集ウィンドウ: 1 秒ごとの `QTimer` で `viewport.update()` を呼んで赤線を移動。`datetime.now().weekday()` の変化を検知したら `_refresh_table()` で列着色を全更新
- メイン実行タブ: 既存の `_refresh_status`（1 秒タイマー）に日付跨ぎ検出を追加。表示中の曜日が「今日」と一致していたら、新しい曜日に自動的に移動

### シーン編集 UI を日本語表示に統一

**背景:** ステップタイプの内部名（`tap_image` / `wait_fixed` / `call_scene` 等）が UI 上でもそのまま表示されており、専門的な印象になっていた。JSON 内部は互換性のため英語のまま、**表示のみ日本語化**。

**対訳:**

| 内部名 | 表示 |
|------|------|
| `wait_fixed` | 待機 |
| `snapshot` | 画像出現待ち |
| `tap` | タップ |
| `tap_image` | 画像をタップ |
| `swipe` | スワイプ |
| `scroll` | スクロール（ジッター付き） |
| `call_scene` | シーン呼び出し |
| `if_image` | 画像で分岐（成立→ / 不成立→） |
| `pick_scene` | シーン抽選[ランダム / 順番] |
| `keyevent` | キー入力 |
| `group_header` | 見出し |

**対象:**
- `_step_label`: ステップ一覧の表示
- ドラッグメニュー（左クリック範囲選択後）
- 「+ その他のステップ ▾」メニュー
- 各種ダイアログタイトル（待機 / シーン呼び出し / シーン抽選 / キー入力 / 見出し / 画像で分岐）
- `pick_scene` のモード選択も「ランダム / 順番」

---

## 2026-05-23（ウォッチャー実装・ステップ拡張・ログ統合）

### ウォッチャー機能を PC 版に新規実装

**背景:** mobile 版にあった「画面を監視して条件を満たしたら割り込みシーンを実行」する仕組みを PC 版にも導入。テンプレマッチ・OCR・連続ヒット判定をエディタ内で構築し、フロー実行と連動できる形に。

**新規ファイル:**
- `pc/gui/pc_watcher.py`: `PcWatcher` / `WatcherCondition` データクラス、`evaluate_watcher` (`image_appear` / `image_gone` / `ocr_number` の 3 タイプ)、`EvalResult`、JSON I/O
- `pc/gui/pc_watcher_editor.py`: 独立編集ウィンドウ（1000×800）。スクショ取得・領域選択・閾値・OCR 設定・ハンドラー/after 動作・優先度/冷却/ポーリング/通知・単発テスト・連続監視テスト・OCR 入力画像の debug 保存

**メインウィンドウ:**
- 見張りタブを placeholder から「ウォッチャー一覧 + 新規/編集/削除」に差し替え
- 一覧の各アイテムに **チェックボックス** を追加（即時 JSON 上書きで有効/無効トグル）

### シーン編集 UI に B 系ステップを追加できるように

**追加ステップ:** `call_scene` / `if_image` / `keyevent` / `scroll` / `group_header` / `pick_scene`

- キャンバスドラッグメニューに `scroll` (ジッター付き swipe) と `if_image` (then/else シーン分岐) を追加
- 右ペインに「+ その他のステップ ▾」メニューを追加し、`call_scene` / `pick_scene` / `keyevent` / `group_header` を選択して追加可能
- ステップ一覧ラベルに新タイプの絵文字 (🌀 scroll / 📞 call_scene / ❓ if_image / 🎲 pick_scene / ⌨ keyevent) を反映

### `pc_scene.py` 実行エンジンに B 系ステップを実装

- `call_scene`: シーン呼び出し。`_MAX_CALL_DEPTH=10` の階層上限と循環参照ガード付き
- `if_image`: `cv2.matchTemplate` で領域内のテンプレ一致を判定し、`then_scene` / `else_scene` を呼ぶ
- `keyevent`: `ctypes.windll.user32.keybd_event` で Win32 キー入力送信（`_KEY_VK_MAP` で esc / enter / f1〜f12 / a-z など）
- `scroll`: `swipe` 動作にジッターを適用（座標 ±jitter / duration ±jitter_ms）
- `group_header`: 表示用の no-op
- `pick_scene`: `random` / `sequential` モードで scenes リストから 1 シーン選択して呼ぶ。`sequential` はプロセス内の `_pick_counters` で順番管理

### `PcFlowRunner` にウォッチャー統合（A1〜A4）

**処理フロー:**
1. フロー開始時に `list_pc_watchers()` で有効なウォッチャーを全部読み込み、別スレッドで監視開始
2. シーン実行中も並行ポーリング: ランダム間隔 (`poll_min_s` 〜 `poll_max_s`) で全 watcher を評価
3. `consecutive` 回連続ヒットで発火 → 発火キュー追加 + `_watcher_pending` セット → `run_pc_scene` の `should_stop` が True → シーン中断
4. メインループで発火キューを優先度順で取り出し、`handler` シーン実行（その間 watcher はポーズ）
5. `after` 動作で復帰: `restart_scene` / `next_scene` / `stop` / `noop`

**安全機構:**
- 各 watcher ごとに `cooldown_s` 後しか再発火しない
- `_hit_counts` / `_last_fired` をスレッドセーフに管理
- 例外捕捉でループが落ちない

### OCR 改善

- **複数 PSM 試行**: `ocr_number` 評価で PSM 7 (1行) / 8 (単語) / 6 (ブロック) を順に試して最長数字列を採用
- **EvalResult.note にデバッグ情報**: `crop=120x32 psm=7 var=2` 等
- **テスト時の crop 保存**: `debug/ocr_<id>_<timestamp>.png` に切り出し画像を保存し、ログにパスを表示。実際に OCR にかかっている画像を目視確認できる
- **Tesseract パスを `pc_main.py` でセットアップ**: 経験値メーター GUI と同じく起動時に `apply_path` で `pytesseract.tesseract_cmd` を設定。`run_pc_flow.py` 経由でも OCR が動くようにした

### ログを統合・ローテーション対応

- 新規 `pc/gui/logger.py`: `write_log(msg)` / `purge_old_logs(retain_days)` を提供
- 保存先 `logs/YYYY-MM-DD.log`（mobile 版と同じ形式）
- `PcFlowRunner._log` がファイル書き出しも行うようになり、フロー開始/停止・スケジュール発火・シーン実行・🔥 ウォッチャー発火・ハンドラー実行・after 動作などが全部ファイルに残る
- ウォッチャー編集の監視テストは **発火イベントのみ** ファイルに残す（評価ログは画面のみで冗長を回避）
- 起動時に `purge_old_logs` で 30 日より古いログを削除（`settings.json` の `log_retain_days` で変更可）

### 編集ウィンドウのライフサイクル修正

- `SceneEditorWindow` / `WatcherEditorWindow` に **`saved(str)` / `closed(object)` シグナル** を追加
- メイン側で参照を保持していると `destroyed` が飛ばず一覧が更新されない問題を回避
- 保存ボタン押下で即座にメイン一覧が更新される

### キャンバスズーム / パン

- `PcSnapshotCanvas` に **右クリック保持 + ホイールでマウス位置中心の拡大縮小** (1.0〜10.0 倍)
- 拡大時は **右ボタンドラッグでパン**
- 操作ヒントを編集ウィンドウ右下に表示

### `.gitignore` 追加

- `pc/debug/` を除外（OCR デバッグ画像）

---

## 2026-05-22（シーン編集 UI 実装・ズーム/再生/テスト機能）

### `pc/gui/pc_scene.py` に `tap_image` ステップを追加

**背景:** モバイル版にあるテンプレートマッチ系ステップを PC でも使えるようにしたい。既存の `snapshot` は実質 `wait_image`（テンプレ一致を待つ）として動作していたため、不足していた「画像を見つけてタップ」のみを新規追加。

**実装内容:**
- `tap_image` ステップ: `cv2.matchTemplate(TM_CCOEFF_NORMED)` で検出 → 一致位置の中心 + `tap_offset_x/y` を `mouse.click()`
- パラメータ: `template` (PNG パス), `threshold`, `timeout_s`, `region` (検索範囲を [rx, ry, rw, rh] で絞る), `button`, `duration_ms`, `tap_offset_x/y`
- region による検索範囲指定で誤検出を抑制し、検出も高速化

### `pc/gui/pc_canvas.py` を新規作成

**役割:** シーン編集用のスナップショット表示キャンバス。

- スナップショット PNG をアスペクト比保持で表示
- **クリック** → `clicked(rx, ry)` シグナル（正規化 0.0〜1.0）
- **ドラッグ** → `region_selected(rx, ry, rw, rh)` シグナル
- タップマーカー（黄色 ●）と領域マーカー（青枠）を重ね描き
- **右クリック保持 + ホイール**でズーム（1.0〜10.0 倍、1.2 倍刻み、マウス位置を中心に拡大）
- **右ボタンドラッグ**（拡大時のみ）でパン

### `pc/gui/pc_scene_editor.py` を新規作成（独立ウィンドウ）

**役割:** メインの 1/4 縦長画面とは独立した、約 1000×800 の編集ウィンドウ。

- ヘッダー: シーン名・対象ウィンドウ・保存
- 中央 QSplitter: 左にキャンバス（広め）、右にステップ一覧 + 操作
- **スクショ取得**: `capture_window` → `snapshots/snap_*.png` 保存 + `snapshot` ステップ追加
- **キャンバス左クリック** → `tap` ステップ追加（オプションで `wait_fixed` 連結）
- **キャンバス左ドラッグ** → メニュー: `wait_image` / `tap_image` / `swipe`
  - `wait_image` / `tap_image` はテンプレを `templates/<scene名>/tpl_*.png` に保存
- **「タップ後に待機」チェックボックス + 秒数入力**: ON のときクリック時に `tap` + `wait_fixed` を 2 ステップ連続で追加（デフォルト ON / 1.5 秒）
- ステップ一覧: 削除・↑↓ 移動
- **「選択行を実行」**: ステップ単独実行（バックグラウンドスレッド）
- **「▶ 再生 / ■ 停止」**: シーン全体を順次実行、停止フラグで中断
- 実行ログを QTextEdit に出力（Qt シグナル経由でメインスレッドに通知）
- Pico は `mouse_provider` Callable で常に最新の接続状態を取得（再接続にも追従）

### `pc/gui/pc_main.py` 作成タブを実装

- 「作成」placeholder を **シーン一覧 + 編集起動** に差し替え
- シーン JSON 一覧 (QListWidget)、ボタン: 新規 / 編集… / 複製 / 削除 / 一覧更新
- ダブルクリック / 「編集…」で `SceneEditorWindow` を **別ウィンドウ** として開く
- メインの縦長 1/4 はそのまま、編集だけ広めの画面で行う運用に最適化

### 設計上のポイント

- **座標系**: 全てクライアント領域相対 (0.0〜1.0)。ウィンドウサイズ変化に追従
- **保存先**: シーン = `pc/scenes/<name>.json` / スナップ = `pc/snapshots/snap_*.png` / テンプレ = `pc/templates/<scene名>/tpl_*.png`
- **画像判定**: `cv2.matchTemplate(TM_CCOEFF_NORMED)` を実行エンジン側で統一
- **実行の非同期化**: `threading.Thread` + Qt シグナル。`should_stop` で中断ハンドリング

---

## 2026-05-22（PC GUI 縦長タブ化とテストタブ・ドラッグ戦略確定）

### `pc/gui/pc_main.py` を縦長タブ構成にリファクタ

**背景:** Win11 のスナップで画面 1/4（≈480×1080）に収まる縦長レイアウトが必要だった。あわせて、フロー制御・カーソル/クリック/ドラッグの動作確認・ログ閲覧をひとつのウィンドウで完結させたい。

**実装内容:**

- ウィンドウサイズ: `setMinimumWidth(360)` + `resize(480, 900)`
- 常設ヘッダー: ゲームウィンドウ選択 + Pico マウス接続
- タブ構成: **実行 / テスト / 見張り / 作成 / ログ**
  - 実行: フロー選択・開始/停止・スケジュール一覧・次回予定（旧画面の機能を移設）
  - テスト: カーソル移動・クリック・ドラッグの動作検証（新規）
  - 見張り / 作成: 未実装の placeholder（次フェーズで mobile から移植予定）
  - ログ: フロー実行ログ（クリアボタン付き）
- 経験値メーターセクションを削除（`run_exp_meter.py` で別アプリとして残る）
- アプリ全体フォントを **Segoe UI Medium 10pt** に強化、補足テキストは 12px に底上げ

### テストタブを実装（カーソル移動 / クリック / ドラッグ）

**動作確認用の機能を新規実装:**

- 目標座標 X/Y 入力欄 + 「現在位置」「クリック取得」ボタン
- **クリック取得モード**: 全画面の半透明オーバーレイ（`_ClickCaptureOverlay`）を出し、左クリックを連続取得。右クリック・ESC で終了。ゲームウィンドウがマウスキャプチャしていてもオーバーレイで横取りできる。件数カウンタをリアルタイム表示。
- **移動モード**: 「滑らか (HID 相対)」/「ジャンプ (絶対座標)」のラジオボタン
- **移動速度スライダー** (1〜10): `(max_step, delay)` のペアを計算。最遅 25 px/s〜最速 8000 px/s。表示で目安 px/s も出す
- **HID 移動**: モードに応じて `move_to_accurate`（補正付き）/ `move_cursor`（SetCursorPos）を切り替え
- **キャリブレーション**: `PicoMouse.calibrate()` のラッパー
- **左/右クリック**: `PicoMouse.click()` 直叩き
- **ドラッグ** (開始 X/Y, 終了 X/Y, 各コピーボタン): ハイブリッド戦略（後述）
- すべて **Pico 接続時のみ有効**（接続前はグレーアウト）
- 実行ログをタブ内に表示

### ドラッグの動作戦略を確定

**問題:** ドラッグ移動を `SetCursorPos`（瞬間移動）でやると、Raw Input にマウス移動イベントが届かないためゲームがドラッグと認識せず単発クリック扱いになる。一方、開始位置移動はジャンプでも問題なく、TeamViewer も実際そう動作している。

**確定した戦略（`docs/pc_pico_mouse.md` に追記）:**

| 工程 | 滑らかモード | ジャンプモード |
|------|--------------|----------------|
| 1. 開始位置へ移動 | `move_to_accurate`（スライダー速度 + 補正） | `SetCursorPos` で瞬間移動 |
| 2. 一呼吸 (200ms) | ✓ | ✓ |
| 3. 左ボタン押下 | ✓ | ✓ |
| 4. 終了位置へ移動 | **HID 相対 固定 160 px/s + イーズアウト** | **HID 相対 固定 160 px/s + イーズアウト**（必ず滑らか） |
| 5. 左ボタン解除 | ✓ | ✓ |

**パラメータ定数:**
- `_DRAG_MAX_STEP = 8` / `_DRAG_DELAY = 0.05` → 160 px/s
- `_DRAG_PAUSE_MS = 200` → 開始位置到達後の一呼吸

**設計上の判断:**
- ドラッグ移動 (工程 4) は **モードによらず必ず滑らか**: Raw Input にマウス移動イベントを発火させる必要があるため
- 開始位置はジャンプ OK: TeamViewer も同じ動作で Nightcrows のマクロ判定に引っかからないことを確認
- 一呼吸 200ms: press 前にカーソルを安定させてゲーム側がドラッグ準備を整える時間

### `_test_drag` の高精度化

- **開始位置の誤差補正**: スライダー速度のまま `move_to_accurate` で目標まで詰める。残差・補正回数をログ出力
- **ジャンプモードの開始位置**: `move_cursor` で SetCursorPos → 誤差 0
- **ドラッグ移動の精度**: `move_to` のイーズアウト（`dist // 3` でステップ縮小、最終 min_step=1px）で終端減速

---

## 2026-05-22（PC フロー制御プログラム実装）

### PC フロー制御プログラムを実装（ステップ 1〜4）

**背景:** 設計書（`docs/pc_flow_design.md`）に基づき、PC 版のフロー自動実行システムを実装した。mobile 版の ADB 操作を Pico HID マウス + Win32 キャプチャに置き換えた PC 専用システム。

**実装内容:**

- `pc/gui/pc_scene.py`: シーン実行エンジン。`snapshot`（テンプレートマッチング待機）/ `tap`（Pico クリック）/ `swipe`（Pico プレス＋移動＋リリース）/ `wait_fixed` の4ステップタイプを実装。座標はウィンドウ相対比率（0.0〜1.0）で指定し、毎ステップ `win32gui` で絶対座標に変換。
- `pc/gui/pc_flow.py`: スケジューラー。`PcFlowRunner`（`QObject`）がバックグラウンドスレッドで時刻を監視し、発火時に `pc_scene.run_pc_scene` を呼び出す。mobile 版と同じ JSON フォーマットを mobile への依存なしで再実装。Qt シグナル（`log_message` / `scene_started` / `step_updated` / `state_changed` / `next_schedule_changed`）でメインスレッドに通知。
- `pc/gui/pc_main.py`: メインウィンドウ（PySide6）。ゲームウィンドウ選択・Pico 接続・フロー開始/停止・スケジュール一覧・次回予定表示・経験値メーター統合のセクションで構成。
- `pc/run_pc_flow.py`: エントリーポイント。`_ensure_cwd()` で実行ディレクトリを統一（PyInstaller exe にも対応）。
- `pc/flows/基本_pc.json`: フロー JSON テンプレート（スケジュール空、シーン追加用）。
- `pc/scenes/`, `pc/flows/` ディレクトリを作成。

**設計上の判断:**

- **swipe の duration_ms 制御:** `n_steps = dist // 15`、`step_delay = duration_ms / 1000 / n_steps` で PicoMouse.move_to() の遅延を計算。HID イーズアウトと組み合わせて自然なスワイプを実現。
- **Pico 未接続フォールバック:** インポートを `try/except ImportError` でラップし、Pico なしでも GUI を起動可能。tap/swipe のみスキップ。
- **起動時スケジュールスキップ:** 起動時刻より前のエントリを `last_fired` に事前登録し重複実行を防止。

**設計書:** `docs/pc_flow_system.md` を新規作成（アーキテクチャ図・JSON フォーマット仕様・依存関係を含む）。

**環境対応:** `.venv` に `pywin32` が未インストールだったため `pip install pywin32` を実施。

---

## 2026-05-22（Pico HID マウス）

### Raspberry Pi Pico による物理マウス入力実装

**背景:** `SendInput` は Nightcrows の `LLMHF_INJECTED` チート検知で弾かれることを確認。Pico を USB HID デバイスとして使うことで物理入力相当を実現。

**実装内容:**

- `pc/pico/code.py`: CircuitPython ファームウェア。コマンド: `PING` / `HOLD` / `RELEASE` / `CLICK` / `MOVE`
- `pc/pico_mouse.py`: PC 側コントローラ `PicoMouse`。キャリブレーション・イーズアウト移動・フィードバック補正を実装
- `pc/move_test.py`: 対話テストツール（c/1〜8/q の 9 モード）
- 設計書: `docs/pc_pico_mouse.md`

**移動精度の仕組み:**

1. `calibrate()`: HID 60 単位送信 → 実移動画素数 → `speed_scale` を算出
2. `move_to()`: イーズアウト（残距離 // 3 でステップを自動縮小）+ speed_scale 補正
3. `move_to_accurate()`: `move_to` 後に `GetCursorPos` でフィードバック補正（最大 8 回）

キャリブレーション済みで誤差 ±1〜3px、1〜3 回補正で収束。

---

## 2026-05-22

### 経験値計測の OCR 誤読耐性を強化（`pc/gui/exp_meter.py`）

**背景:** PC版経験値メーターのログを精査したところ、OCR 誤読 1 点が累積に大きく水増しされる事故が発生していた。例として 5-22 のログでは、誤読 `76.5880%` が混入した結果、累積が +37%（さらに直後の LvUP 誤検知連鎖で +57%）と、**1 回の誤読で累積が約 +94% 暴走**していた。

**実装内容:**

- 内部状態を `samples`/`prev_raw`/`accumulated` から **`_raw_samples`（生値のみ）** に変更し、平滑化値・累積・時速はすべて派生計算する設計に変更
- `_filtered_series()` を追加: 直近 `MEDIAN_WINDOW=5` サンプルの **中央値フィルタ** で外れ値を除去
- `_cumulative_series()` を追加: 平滑化系列の隣接Δを積み上げ、`LVUP_DROP_THRESHOLD=30%` 以上の落差で LvUP 判定
- `current_speed()` を **直近 10 個の隣接Δレート（%/h 換算）の中央値** に変更（旧: 直近3サンプルの累積差）。LvUP境界補正・微小マイナス無視・サンプル間隔の dt 正規化込み
- 外部 API は `prev_raw`/`samples`/`accumulated` を read-only property で互換維持し、`main.py`/`overlay.py` 側に変更なし
- 永続化フォーマットを `raw_samples` のみに変更。旧形式（accumulated/samples/prev_raw）は生値復元不可のため起動時に破棄

**検証結果（5-22 ログを再生）:**

| 指標 | 旧アルゴリズム | 新アルゴリズム |
|------|---------------|---------------|
| 最終累積 | 100.29%（暴走） | 6.15%（妥当） |
| current_speed 中央値 | 1.16 %/h | 1.16 %/h（変わらず） |
| 標準偏差 | 283.6 %/h | **0.351 %/h** |
| 最大値 | 3864 %/h | 2.99 %/h |
| >10%/h の異常値 | 17 個 | **0 個** |

中央値ベースは正しく、ブレが 800 倍改善された。

### PC 用ツールフォルダ整理と README 整備

- `pc/build_exe.bat` / `pc/run_exp_meter.py` / `pc/gui/` を `577704f` で追加していた範囲を確定
- ルートに `run_pc.bat` を追加、`requirements.txt` に `pywin32>=308` を追加
- `.gitignore` で `pc/build/`, `pc/dist/`, `pc/*.spec`, `pc/tools/`, `pc/settings.json`, `pc/exp_meter.json`, `pc/logs/` を除外

### PC 自動入力の検証（`pc/tap_test.py` / `pc/click_test.py`）

**背景:** モバイル版で機能している「スクショ→画像判定→タップ」フローを PC 版 Nightcrows でも実現したい。タップ送信部だけ Windows API に置き換える方針で検証した。

**結果:** 詳細は `docs/pc_input.md` 参照。要点:

- **`SendInput` 合成マウスクリック**: メモ帳等では動くが、**Nightcrows は無反応**。`LLMHF_INJECTED` を見たチート対策で弾かれている
- **`InjectTouchInput` (Touch Injection API)**: TeamViewer 経由のリモートでは `ERROR_INVALID_PARAMETER (87)` で失敗。リモートセッション制限の可能性が濃厚で、ローカル実行ができないため未確定
- **TeamViewer 経由の人手マウス操作**: 通る（専用ドライバが物理入力として注入するため）

**結論:** 合成入力は弾かれる。残る選択肢は (a) Interception 等の署名ドライバ（規約違反/BAN リスクあり）、(b) ローカルで Touch Injection 再検証、(c) Arduino/HID 物理デバイス、(d) mobile/ の adb 版継続。

---

## 2026-05-21

### 経験値計測パネルの追加（`gui/watcher_editor.py`）

**背景:** 狩り中に経験値%の成長速度をリアルタイムで確認する手段がなかった。またレベルごとに必要経験値が変わるため、手動リセットで基準をリセットできる機能が必要だった。

**実装内容（`gui/watcher_editor.py` のみ）:**

- `_RegionPickerDialog` を追加。スクショ or ファイルからドラッグで経験値%の表示領域を選択するダイアログ
- `ExpMeterWidget` を追加。ウォッチャータブ下部に固定表示される計測パネル
  - `▶ 計測開始 / ■ 停止`: 5分タイマーの ON/OFF。開始時に即時1回取得
  - `📍 領域設定`: 領域を `exp_meter.json` に保存。再起動後も保持
  - `🔄 リセット`: サンプル列・累積%・計測開始時刻をクリア（計測中なら即再開）
- サンプリング: バックグラウンドスレッドで `screencap` → `_ocr_digits_best`（`flow_runner` 流用）
- 速度計算: 現在速度（直近3サンプル≈15分）・平均速度（全サンプル）を %/h で表示
- 比較表示: 現在速度が平均より速いか遅いかを ↑緑 / ↓赤 で表示
- レベルアップ検知: 前回値との差が -30% 未満の急落でLvUPとみなし `(100-prev)+raw` を累積加算
- OCR誤読対策: 差が -30〜0% のときはスキップ（累積・prev_raw を更新しない）

**設計上の判断:**

- LvUP回数を表示しない: OCR誤読との区別が困難で数字が信頼できないため、速度計算への加算のみに使う
- 平均と現在の2速度で比較: 「今が速いか遅いか」を直感的に判断できる形にした
- リセット機能を手動のみ: レベルアップで必要経験値が変わる都度ユーザーが手動でリセットする運用

### フロー編集タブの曜日ハイライト日付変更対応（`gui/flow_editor.py`）

**背景:** GUIを起動したまま日付をまたぐと、フロー編集タブの今日列ハイライト（黄色背景）が更新されなかった。

**修正:** `_on_time_tick()`（30秒タイマー）でウィークデーの変化を検知し、旧今日列のプレースホルダーを解除した後 `_highlight_today()` を呼ぶ。`_unhighlight_day(col)` を追加。

---

## 2026-05-11

### ウォッチャー発火回数の表示（`gui/watcher_editor.py`）

**背景:** ウォッチャーが1日の中で何回・何時に発火したか確認する手段がログ閲覧しかなかった。ウォッチャータブを見れば即座に把握できるようにした。

**実装方針:** `flow_runner.py` / `runner_widget.py` は変更せず、既存の `logs/YYYY-MM-DD.log` をパースして発火回数を取得する。アプリを再起動しても今日分のカウントが保持される。

**実装内容（`gui/watcher_editor.py` のみ）:**

- `_parse_today_fire_log()` を追加。今日のログファイルから `👁 watcher 発火: [タイトル]` 行を正規表現でパースし `{title: ["HH:MM", ...]}` を返す
- `_make_item()` に `fire_times: list[str] | None` 引数を追加。発火があれば `🔥 本日 N回  最終: HH:MM` を3行目に表示
- `_refresh_list()` で `_parse_today_fire_log()` を呼び出して各アイテムに反映
- `__init__` に 30秒周期の `QTimer` を追加して自動更新

**表示例:**
```
[✓]  体力低下  |  🔢 OCR数値
      → 復活  /  restart_scene  /  優先度:900  冷却:120s
      🔥 本日 3回  最終: 14:37
```

---

## 2026-05-08

### デバイス切断時にフロー・録画を自動停止（`gui/main.py`）

**背景:** デバイスが切断されてもフローや録画がそのまま動き続けることがあった。

**修正:** `_set_connected(None)` に `_stop_on_disconnect()` 呼び出しを追加。実行中のフロー・スケジュール録画・リアル録画をそれぞれ停止し、シーン編集ログに理由を記録する。scrcpy 終了による切断・手動切断のどちらでも動作する。

---

## 2026-05-05

### PC 間同期: PUSH / PULL ボタン追加（`gui/main.py`, `gui/flow_editor.py`）

**背景:** シーン・フロー・ウォッチャーをオフィスと自宅の2台の PC で手動同期していた。git コマンドをターミナルから打つ手間を省くため、GUI 上部バーに PUSH / PULL ボタンを追加した。

**実装内容:**

- `gui/main.py`:
  - 上部バーに「↑ PUSH」「↓ PULL」ボタンを追加（`⚙` 設定ボタンの左）
  - `sync_result_signal = Signal(bool, str)` を追加
  - `_sync_push()`: `git add scenes/ flows/ watchers/` → 差分チェック → `git commit` → `git push` をバックグラウンドスレッドで実行
  - `_sync_pull()`: `git pull` をバックグラウンドスレッドで実行、完了後にウォッチャー・フローをリロード
  - `_on_sync_result()`: シグナル受信でボタン再有効化 + ログ表示 + PULL 後のリロード
- `gui/flow_editor.py`:
  - `reload_current_flow()` を追加: `_flow_path` が設定済みの場合にディスクから再読込してグリッドを再描画

**設計上の判断:**
- git を同期手段に採用: このプロジェクトはすでに git/GitHub で管理されており、追加インフラ不要
- 差分なしの場合は `git commit` をスキップして `git push` のみ: 「変更なし」をエラーにしない
- PULL 後は自動リロード: フロー・ウォッチャーはメモリ上のオブジェクトなのでディスク読み直しが必要

---

## 2026-05-01〜02

### バグ調査・修正: 「続けての処理」が実施されない（`flows/基本.json`, `gui/flow_editor.py`）

**背景:** 金曜 13:07 のスケジュール発火ログが `→ ['近隣の街でポーション補給.json']`（1件）となっており、続けて設定されていた「スケジューラー起動」が実行されなかった。

**根本原因:** 月・火・木・日のエントリには `"sequence": ["スケジューラー起動.json"]` が設定されていたが、金曜（`days=[4]`）・土曜（`days=[5]`）のエントリには `sequence` フィールドが存在しなかった。コピーして作成した際、コピー元が sequence 未設定のエントリだったため欠落が引き継がれた。

また日曜エントリには `sequence` に `近隣の街でポーション補給.json` が target と重複して含まれており、UI 上に3行表示されていた。

**修正:**

- `flows/基本.json`: 金曜・土曜エントリに `"sequence": ["スケジューラー起動.json"]` を追加。日曜エントリの `sequence` から重複の `近隣の街でポーション補給.json` を削除。
- `gui/flow_editor.py` `_entries_from_schedule()`: `sequence` のアイテムが `target` と同名の場合に seq エントリ追加をスキップ。コピーペーストで重複が伝播しないよう防御。

**複製ロジックとの関係:** コピー・ペースト処理自体に誤りはなく、コピー元データが壊れていたことが直接原因。防御コードで今後の再発を抑止。

---

### OCR 精度改善: 前処理マルチバリアント（`gui/flow_runner.py`, `gui/ocr_test_dialog.py`）

**背景:** `_ocr_number` が Otsu 二値化の1通りのみで OCR を行っていた。ゲーム UI はグラデーション・光沢背景が多く Otsu 閾値が外れるケースがあり、正常値 39720 が 21 や 73 と誤読されることがあった。

**修正内容:**

#### `gui/flow_runner.py`

- `_preprocess_for_ocr(crop)` を追加。4バリアントを生成してリストで返す:
  - `[0]` Otsu 二値化（従来）
  - `[1]` Otsu 反転 — Otsu が明暗を誤判定したとき（明色テキスト on 暗色背景など）の救済
  - `[2]` ガウシアンぼかし後 Otsu — アンチエイリアス・ノイズを平滑化してから二値化
  - `[3]` 適応的二値化 — グラデーション背景・局所コントラストに強い
- `_ocr_digits_best(crop, config)` を追加。全バリアントで OCR を試し、**最も桁数の多い数字列**を採用（短い誤読より長い正読を優先するヒューリスティック）。返値は `(digits_str, variant_index)` のタプル。
- `_ocr_number` / `_read_ocr_value` を `_ocr_digits_best` を使うよう書き直し、コードを大幅に削減。
- `_OCR_VARIANT_NAMES = ["Otsu", "Otsu反転", "Otsu+ぼかし", "適応的"]` を定義（テストダイアログと共有）。

**設計上の判断:**
- 全バリアントを必ず試して最長採用（`best` 方式）を採用。先頭成功で打ち切る `first` 方式より精度が高く、OCR の速度（~100ms/回）と 1〜10s ポーリング間隔のバランスから許容範囲内。

#### `gui/ocr_test_dialog.py`

- **「前処理後 (Tesseract入力)」プレビューを追加** — テストダイアログ下部に実際に Tesseract に渡すバイナリ画像を表示。実行時と同じ前処理結果を肉眼で確認できる。
- `_run_ocr()` を `_preprocess_for_ocr` / `_ocr_digits_best` を使う実装に統一。テスト結果に採用バリアント名と全バリアントの読み取り値を表示:
  ```
  読み取り結果: 39720  [Otsu+ぼかし]
  Otsu: 39720  |  Otsu反転: —  |  Otsu+ぼかし: 39720  |  適応的: 39720
  ```

---

### スクショ部分破損の検知（`gui/flow_runner.py`）

**背景:** `screencap()` は ADB エラー・None デコードを正しくスキップしていたが、PNG ヘッダーが正常でも末尾が欠けた部分破損データは `cv2.imdecode` が「成功」してしまい、画面上部しか写っていない画像で OCR が走る可能性があった。

**修正:** `WatcherState._run()` のデコード後に画像サイズを検証。200px 未満の場合は「スクショ異常サイズ」をログに出力してスキップ。

---

### 実行ログのファイル出力・ローテーション（`gui/runner_widget.py`）

**追加内容:**

- **`logs/YYYY-MM-DD.log`** にログを自動追記（UTF-8、1行1エントリ）。
- 日付をまたいで実行中の場合、午前0時以降の最初のログ書き込み時に新ファイルへ自動切り替え。
- **30日より古い `.log` ファイルを自動削除**（新しい日付ファイルを開くタイミングで実行）。保持日数は `_LOG_RETAIN_DAYS = 30` で変更可能。
- アプリ終了時（`shutdown()`）にファイルハンドルをクローズ。

**ディレクトリ:** プロジェクト直下の `logs/`（起動時に自動作成）。

---

### バグ修正: 録画停止後にボタンが「録画」に戻らない（`gui/recorder_widget.py`）

**症状:** 上部の「📹 録画」ボタン押下後「■ 録画停止」になるが、もう一度押しても「📹 録画」に戻らない。

**原因:** `ScreenRecorder.stop()` はスレッドに停止フラグを立てるだけで非同期。ボタン押下直後に `_update_rec_buttons()` が `is_recording()` → `_thread.is_alive()` を確認するとスレッドがまだ生きており `True` を返すため、ボタンが "録画停止" のままになった。その後 `_refresh_status()` の自動停止検知は `btn_start` がすでに有効化されているため再発火せず、永続的に戻らなかった。

**修正:** `stop_recording()` 内で `self._recorder.stop()` 呼び出し直後に `self._recorder = None` をセット。これにより `is_recording()` が即 `False` を返すようになり、続く `state_changed.emit(False)` → `_update_rec_buttons()` が正しく "📹 録画" に戻す。自動停止検知（スレッド自然終了）には影響なし。

---

## 2026-04-27

### 設定: `last_flow` を相対パスで保存し PC 間ポータブルに（`gui/settings.py`）

**背景:** 2台の PC でリポジトリを共有しているが `settings.json` の `last_flow` が絶対パス（例: `D:/github2/nightcrows/flows/基本.json`）で保存されるため、もう一方の PC では起動時に前回のフローが復元されなかった。

**修正:**

- `_to_relative_path(p)`: 絶対パスをプロジェクト相対パス（例: `flows/基本.json`）に変換。異なるドライブへの参照は絶対パスのままフォールバック。
- `_to_absolute_path(p)`: 読み込み時に相対パスを `os.path.abspath` で絶対パスに展開。
- `save_settings` で `last_flow` を相対パスに変換してから書き出し。
- `load_settings` で読み込んだ値を絶対パスに展開して `AppSettings` に格納。

既存コード（`os.path.exists` 等）への影響なし。

---

### ウォッチャー編集：画像マッチテストボタン追加（`gui/watcher_editor.py`）

**背景:** OCR 条件には「▶ OCRテスト」ボタンがあるが、`image_appear` / `image_gone` 条件にはスコア確認手段がなく、閾値の妥当性を検証できなかった。

**追加内容:**

- `image_appear` / `image_gone` パネルそれぞれに「**▶ マッチテスト（手動実行）**」ボタンとマッチ結果ラベルを追加。
- `_run_match_test()` メソッド: `cv2.matchTemplate` でスコアを計算し、閾値との比較結果を `✅ 発火 / ❌ 不発火  スコア: X.XXX  マージン: ±X.XXX` 形式で表示。
- **自動実行:** スクショ取得後・領域ドラッグ選択後に自動でテストを実行。

**使い方:** 1. スクショ取得 → 2. マッチ結果ラベルで即時確認 → 3. 必要に応じて閾値を調整して再テスト。

---

### バグ修正: スケジュール `target` と `sequence` の混在問題（`gui/flow.py`, `flows/基本.json`）

**問題:** `ScheduleEntry` の `sequence` に追加でシーンを登録すると、実行時コード `scenes = entry.sequence or ([entry.target] if entry.target else [])` が `sequence` を優先するため、旧形式の `target` フィールドに残っていたシーンが完全に無視されていた。

**再現ケース:** 月曜 13:08 のスケジュール — `target=近隣の街でポーション補給.json`、`sequence=[スケジューラー起動.json]` という状態で、ポーション補給が実行されずスケジューラー起動だけが動いた。

**修正:**

- `gui/flow.py` `_schedule_from_dict`: 読み込み時に `sequence` が非空かつ `target` が未含有の場合のみ `target` を先頭挿入（自動マイグレーション）。
  - `sequence` が空の場合は既存の `or` フォールバックに任せ、挿入しない（二重表示防止）。
- `flows/基本.json`: 月・日曜 13:08 エントリの `sequence` を `["近隣の街でポーション補給.json", "スケジューラー起動.json"]` に修正。

---

### 実行ログにシーン名を表示（`gui/flow_runner.py`）

**変更前:** `▶ スケジュール [1/1]: スケジューラー起動.json`
**変更後:** `▶ スケジュール [1/1]: スケジューラー起動  (スケジューラー起動.json)`

`run_scene` でシーン読み込み後に `scene.name`（JSON 内の `name` フィールド）をファイル名の前に表示するよう変更。読み込み失敗時はファイルパスのみ表示。

---

### バグ修正: `scenes/` プレフィックスの二重付与（`gui/flow_runner.py`）

**問題:** ウォッチャーの `handler` フィールドに `"scenes/watcher_appear.json"` のように `scenes/` 付きで保存されているとき、`_scene_path` が `os.path.join("scenes", "scenes/watcher_appear.json")` = `"scenes\\scenes/watcher_appear.json"` を生成してファイルが見つからなかった。

**修正:** `_scene_path(rel)` で `rel` が `scenes/` または `scenes\` で始まる場合にプレフィックスを除去してから `os.path.join` を適用。どちらの形式で保存されていても正しく動作する。

---

## 2026-04-25

### フロー編集：セル複製・スキップ機能追加

#### セルのコピー＆ペースト（`gui/flow_editor.py`）

- グリッドセルを右クリック → **「📋 コピー」** でエントリをバッファに保存
- 別のセルを右クリック → **「📌 貼り付け」** でバッファ内容を貼り付け
- 貼り付け先スロットの時刻に `timed` エントリの時刻を自動更新
- バッファはセッション中保持されるため連続貼り付け可能

#### スケジュールエントリの有効/無効切替（`gui/flow.py`, `gui/flow_editor.py`, `gui/flow_runner.py`）

- `ScheduleEntry` に `enabled: bool = True` フィールドを追加
- JSON には `"enabled": false` のときのみ書き出す（後方互換）
- グリッドセルを右クリック → **「⊘ 無効化（スキップ）」** でその時間枠を一時停止
  - 無効セル：グレー背景・`⊘` マーク表示
  - 再度右クリック → **「✓ 有効に戻す」** で復元
- `flow_runner._check_schedule` / `_last_due_scenes` で無効エントリをスキップ

**用途:** 「今日だけこの時間帯をスキップしたい」といった一時的な除外に使う。

---

### フロー編集：現在時刻横棒のズレ修正（`gui/flow_editor.py`）

**問題:** スクロール・セル編集（行高さ変更）・タブ切り替え後に赤い現在時刻横棒がずれて表示された。

**原因:** オーバーレイ（`_TimeLineOverlay`）が 30 秒タイマーか明示的な `refresh_time_line()` 呼び出し時しか再描画されなかった。スクロールや行高さ変化はビューポートの座標を変えるが、オーバーレイは自動的に再描画されなかった。

**修正:**

| トリガー | 対応 |
|---------|------|
| スクロール | `verticalScrollBar().valueChanged` に `refresh_time_line` を接続 |
| セル編集で行高さ変化 | `verticalHeader().sectionResized` に接続、セル編集後にも呼び出し |
| タブ切り替えで戻る | `FlowEditorWidget.showEvent` で `refresh_time_line()` を呼び出し |

また、デバッグ用に赤線の右に Python が認識している現在時刻（`HH:MM:SS`）を表示するラベルを追加。

---

### ウォッチャー編集：領域枠が表示されない問題の修正（`gui/watcher_editor.py`）

#### 画像ウォッチャー再編集時の枠（`image_appear` / `image_gone`）

**問題:** テンプレート画像（切り抜き済み小画像）をキャンバスに表示した後、元スクショ上の座標（例: `[800, 400, 200, 100]`）で `highlight_region` を呼んでいたため、テンプレート画像の外に枠が描画されて見えなかった。

**修正:** テンプレート画像全体 `(0, 0, w, h)` をハイライト対象に変更。テンプレート = 切り抜いた領域そのものなので視覚的に正しい。

#### OCR ウォッチャー再編集時の枠（`ocr_number`）

**問題:** 「📷 スクショ取得」が `keep_region=False` で `_load_screenshot` を呼んでいたため、既存 `self._region` が復元されなかった。

**根本原因:** `set_image()` 内の `reset_zoom()` でレイアウト確定前にスケール計算が走り、`_base_scale = 0` になるタイミングがあった。その状態で即座に `highlight_region` を呼んでも座標変換結果がすべて 0 になり枠が描画されなかった。

**修正:**
- `_capture` / `_open_file` / `_prefill` の全経路で `highlight_region` 呼び出しを `QTimer.singleShot(50ms)` で遅延
- Qt のイベントループが一周してレイアウト・スケール計算が確定した後にハイライトを設定することで確実に枠が表示される

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
