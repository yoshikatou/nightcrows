# GUI アーキテクチャ

PySide6 製のシーンエディタ兼ランナー。`gui/` 配下 + `run_gui.py` で起動。

## 起動

```
.venv/Scripts/python.exe run_gui.py
```

依存は `requirements.txt`（PySide6 / opencv-python / numpy）。`.venv/` はプロジェクト直下に置く（gitignore 対象）。

## ウィンドウ構成（タブ）

```
┌─────────────────────────────────────────────┐
│ デバイス: [ オフィス ▼ ]  [接続] [切断] [⚙]  │ ← 接続バー（全タブ共通）
│ ✓ 接続中: 192.168.0.119:35103              │
├─── シーン編集 ── フロー編集 ── ランナー ─── │
│                                              │
│   ( タブごとに切り替わる )                    │
│                                              │
└─────────────────────────────────────────────┘
```

- `gui/main.py`: 接続バー + `QTabWidget` のシェル。接続状態（`current_serial`）を保持
- `gui/scene_editor.py` (`SceneEditorWidget`): シーン編集タブの実体。キャンバス + ステップ列 + 記録/再生
- フロー編集タブ、ランナータブは現時点でプレースホルダ

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

## シーン/フロー JSON

- シーンの詳細（ステップタイプ、JSON 構造）は `gui/scene.py` を参照
- フローの詳細は `docs/flow_design.md` を参照

## 関連ドキュメント

- [フロー設計](flow_design.md)
- [2拠点開発環境](dev_environment.md)
- [タッチ入力の実装](touch_input.md)
