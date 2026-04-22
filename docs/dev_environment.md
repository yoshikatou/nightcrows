# 開発環境（2拠点）

nightcrows は自宅とオフィスの2拠点で開発している。拠点ごとに ADB 接続先の IP / ポートが異なる。

## デバイス一覧

| 拠点 | IP | モデル | 解像度 |
|------|----|----|--------|
| オフィス | `192.168.255.57` | - | 1220×2712 |
| 自宅 | `192.168.0.119` | XIG04 | 1220×2712 |

- 解像度が同じなので、座標定数（例：ハンバーガーメニュー `(2582, 60)`）は拠点間で共通で使える
- ポート番号は Android のワイヤレスデバッグが毎回変える可能性があるため、**IP だけを `settings.json` に登録**しておき、接続時に `discover_and_connect` が mDNS とポートスキャンで自動検出する（`docs/gui_architecture.md` 参照）
- 以前のオフィス分ポート `34497`、自宅分ポート `35103` は固定値ではないので覚える必要はない

## ハードコード SERIAL の残骸

`tests/test_*.py` と `docs/touch_input.md` にはオフィス時代のシリアル `192.168.255.57:34497` がハードコードされている（変数 `SERIAL`）。

- 作業開始時は **`adb devices` で現在の接続状態を確認**する
- テストスクリプトを実行するときは該当行を書き換えるか、将来的には環境変数 `ADB_SERIAL` から読むように置き換える（`gui/adb.py:13` の `DEFAULT_SERIAL` は既に環境変数対応済み）

## 依存と起動

- `.venv/`（プロジェクト直下、gitignore 対象）に PySide6 / opencv-python / numpy
- 起動: `.venv/Scripts/python.exe run_gui.py`
- 設定ファイル: `settings.json`（gitignore 対象、デバイス一覧を保存）は初回起動時にオフィス/自宅の2デバイスで自動生成される

## 拠点を跨ぐときの注意

- `settings.json` は gitignore 対象なので、別 PC では再生成される（または手動で同期）
- `scenes/` と `flows/` と `templates/` は git で同期される
- `recordings/` は gitignore 対象（生記録ファイル置き場）

## 関連ドキュメント

- [フロー設計](flow_design.md)
- [GUI アーキテクチャ](gui_architecture.md)
- [タッチ入力の実装](touch_input.md)
