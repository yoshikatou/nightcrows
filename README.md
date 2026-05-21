# nightcrows

ゲーム自動化ツール群。`mobile/` に Android 用（WiFi ADB でタップ記録・再生、シーン切替、PySide6 GUI）、`pc/` に PC 用ツールを格納する。

## 起動

### モバイル用ツール

ルートから:

```
run_mobile.bat
```

または手動で:

```
cd mobile
..\.venv\Scripts\python.exe run_gui.py
```

依存インストール: `pip install -r requirements.txt`

### PC 用ツール

準備中（`pc/` 配下に実装予定。最終的には exe にビルドして配布）。

## ドキュメント

設計判断と運用ルールはすべて `docs/` 配下に md ファイルで残してある（2拠点でのコード同期を前提）。

- [フロー設計](docs/flow_design.md) — Flow/Watcher/Schedule の JSON スキーマ、ウォッチャーの挙動、フォルダ規約、実装状況
- [GUI アーキテクチャ](docs/gui_architecture.md) — タブ構成、scrcpy 外部起動方針、座標変換、接続フロー
- [2拠点開発環境](docs/dev_environment.md) — 自宅/オフィスのデバイス情報、ポート自動検出、設定同期
- [作業履歴](docs/changelog.md) — セッションごとの変更点・決定事項
- [タッチ入力の実装](docs/touch_input.md) — `adb shell input` 方式、getevent による検出

## ディレクトリ

```
mobile/        Android 自動化ツール（PySide6 GUI、ADB）
  ├─ gui/         MainWindow / SceneEditorWidget / ADB / recorder / replay …
  ├─ scenes/      シーン定義（JSON）
  │  ├─ main/       メインシーケンス用
  │  └─ handlers/   割り込みハンドラ用
  ├─ flows/       フロー定義（JSON）
  ├─ watchers/    ウォッチャー定義（JSON）
  ├─ templates/
  │  ├─ snapshots/  シーンエディタが保存するスナップ
  │  ├─ watchers/   監視用テンプレ画像
  │  └─ digits/     OCR 用の 0.png〜9.png
  ├─ tests/       タップ検証スクリプト
  ├─ recordings/  画面録画出力（gitignore）
  ├─ logs/        実行ログ（gitignore）
  ├─ debug/       デバッグ用（gitignore）
  ├─ run_gui.py   GUI エントリーポイント
  ├─ settings.json デバイス・録画設定（gitignore）
  └─ exp_meter.json
pc/            PC 用ツール（準備中）
docs/          設計ドキュメント（共通）
run_mobile.bat モバイル GUI 起動ショートカット
```
