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

### PC 用ツール（経験値メーター）

ルートから:

```
run_pc.bat
```

または手動で:

```
cd pc
..\.venv\Scripts\python.exe run_exp_meter.py
```

#### exe ビルド

配布用の単一 exe を生成する場合:

```
cd pc
build_exe.bat
```

`pc/dist/ExpMeter.exe` が生成されます（約100MB）。配布時は exe 単体でOK。

**配布時の注意**:
- 受け取った人にも Tesseract のインストールが必要（exe 起動時に未検出なら案内ダイアログが出る）
- `settings.json` / `exp_meter.json` / `logs/` は exe と同じフォルダに生成される
- 初回起動はテンプフォルダへの展開で数秒かかる

#### 事前準備: Tesseract OCR のインストール

経験値読み取りに [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) が必要です。

- **インストーラー**: 上記ページから `tesseract-ocr-w64-setup-*.exe` をDL・実行
- **winget**: `winget install --id UB-Mannheim.TesseractOCR -e`

既定の場所にインストールすればアプリが自動検出します。別の場所にインストールした場合は設定ウィンドウから「変更…」で tesseract.exe を指定してください。

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
