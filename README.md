# nightcrows

Android ゲームの自動化ツール。WiFi ADB 経由でタップを記録・再生し、複数シーンを時刻や画面状態に応じて切り替える。PySide6 製の GUI。

## 起動

```
.venv/Scripts/python.exe run_gui.py
```

依存インストール: `pip install -r requirements.txt`

## ドキュメント

設計判断と運用ルールはすべて `docs/` 配下に md ファイルで残してある（2拠点でのコード同期を前提）。

- [フロー設計](docs/flow_design.md) — Flow/Watcher/Schedule の JSON スキーマ、ウォッチャーの挙動、フォルダ規約、実装状況
- [GUI アーキテクチャ](docs/gui_architecture.md) — タブ構成、scrcpy 外部起動方針、座標変換、接続フロー
- [2拠点開発環境](docs/dev_environment.md) — 自宅/オフィスのデバイス情報、ポート自動検出、設定同期
- [作業履歴](docs/changelog.md) — セッションごとの変更点・決定事項
- [タッチ入力の実装](docs/touch_input.md) — `adb shell input` 方式、getevent による検出

## ディレクトリ

```
gui/         PySide6 製 GUI（MainWindow / SceneEditorWidget / ADB / recorder / replay …）
scenes/      シーン定義（JSON）
  ├─ main/     メインシーケンス用
  └─ handlers/ 割り込みハンドラ用
flows/       フロー定義（JSON）
templates/
  ├─ snapshots/  シーンエディタが保存するスナップ
  ├─ watchers/   監視用テンプレ画像
  └─ digits/     OCR 用の 0.png〜9.png
tests/       タップ検証スクリプト
docs/        設計ドキュメント
```
