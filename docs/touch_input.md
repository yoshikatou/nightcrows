# タッチ入力 実装まとめ

## 概要

ADB 経由でAndroidデバイスにタッチ操作を送信する方法を検証した。
当初は scrcpy の control ソケットプロトコルを使用していたが、問題が判明したため `adb shell input` コマンドに切り替えた。

---

## デバイス情報

| 項目 | 値 |
|------|-----|
| シリアル | `192.168.255.57:34497` |
| 物理解像度 | 1220 × 2712 |
| 縦向き（Portrait）論理座標 | 1220 × 2712 |
| 横向き（Landscape）論理座標 | 2712 × 1220（ROTATION_90） |
| ADB パス | `C:\scrcpy\adb.exe` |

---

## 方式比較

### scrcpy control ソケット（不採用）

scrcpy サーバーを起動し、TCP ソケット経由で `INJECT_TOUCH_EVENT` メッセージを送信する方式。

**問題点：**
- `ACTION_DOWN` / `ACTION_UP` のタップは動作するが、`ACTION_MOVE` を使ったスワイプが正常に機能しない（座標がすべて同一点にマッピングされる）
- scrcpy サーバー起動に約5秒かかる
- `pkill -f scrcpy` で既存の scrcpy セッション（画面ミラーリング等）を巻き込んで終了させてしまう問題があった（`pkill -f "scid=2"` に変更して回避）

### adb shell input（採用）

```bash
# タップ（ホールド時間指定）
adb shell input swipe <x> <y> <x> <y> <duration_ms>

# スワイプ
adb shell input swipe <x1> <y1> <x2> <y2> <duration_ms>
```

**メリット：**
- 座標が正確に反映される
- ホールド時間を ms 単位で指定できる
- スワイプが正常に動作する

---

## タッチ操作の実装

### do_tap

```python
def do_tap(ctrl, x, y, screen_w, screen_h, hold_ms=3000):
    subprocess.run(
        [ADB, "-s", SERIAL, "shell", "input", "swipe",
         str(x), str(y), str(x), str(y), str(hold_ms)],
        capture_output=True, timeout=hold_ms // 1000 + 5,
    )
```

- 同座標を `swipe` することでホールド時間を制御する
- `adb shell input tap` はホールド時間を指定できないため不使用

### do_swipe

```python
def do_swipe(ctrl, x1, y1, x2, y2, screen_w, screen_h, steps=50, duration_ms=3000):
    subprocess.run(
        [ADB, "-s", SERIAL, "shell", "input", "swipe",
         str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
        capture_output=True, timeout=duration_ms // 1000 + 5,
    )
```

---

## テストスクリプト

### tests/test_scrcpy_touch.py

| ターゲット | 動作 |
|-----------|------|
| `--target current` | HOME を押さず現在の画面中央をタップ |
| `--target home` | HOME ボタンを押してからホーム画面中央をタップ |
| `--target settings` | 設定アプリを起動して中央・上部をタップ |
| `--target swipe_lr` | 中央 ±300px を左→右にスワイプ（3秒） |
| `--target tap5` | 左上・右上・中央・左下・右下の5点を順にタップ（各3秒・5秒間隔） |
| `--target game` | ゲーム横向き時の右上ハンバーガーメニューをタップ（座標確認済み） |

### tap5 の動作フロー

1. Enter キー入力待ち（準備完了を確認してから開始）
2. 5点を5秒間隔でタップ
   - (305, 678) 左上
   - (915, 678) 右上
   - (610, 1356) 中央
   - (305, 2034) 左下
   - (915, 2034) 右下

---

## タップ表示の確認方法

開発者オプションの「タップ操作を表示」を ADB で有効化できる。

```bash
# 有効化
adb shell settings put system show_touches 1

# 無効化
adb shell settings put system show_touches 0
```

---

## ゲーム画面の確認済み座標

ゲームは横向き（ROTATION_90）で動作。論理座標は 2712 × 1220。

| UI要素 | 座標 | 確認方法 |
|--------|------|---------|
| ハンバーガーメニュー（右上） | (2582, 60) | スクリーンショット解析・タップ後メニュー開放を確認 |

### 座標確認手順

1. `adb exec-out screencap -p > cap.png` でスクリーンショット取得
2. PIL で右上コーナーを切り出して目的のアイコン位置を特定
3. タップ前後のスクリーンショットを比較して動作確認

```bash
# スクリーンショット取得
adb -s 192.168.255.57:34497 exec-out screencap -p > debug/cap.png
```

```python
# 右上コーナー切り出し（PIL）
img = Image.open("debug/cap.png")
crop = img.crop((img.width - 200, 0, img.width, 200))
crop.save("debug/cap_topright.png")
```

> **注意:** `screencap /sdcard/cap.png` 形式はこのデバイスで動作しない。`exec-out screencap -p` を使うこと。

---

## 人間のタップ検出

`adb shell getevent` でデバイスの生タッチイベントをリアルタイムに読み取れる。

### 入力デバイス情報

| デバイス | 用途 |
|---------|------|
| `/dev/input/event3` | タッチスクリーン（確認済み） |
| `/dev/input/event4` | タッチイベントなし（不使用） |

getevent の座標は物理解像度の **10倍** で出力される。

| getevent 範囲 | 論理座標 |
|--------------|---------|
| X: 0 – 12200 | X: 0 – 1220 |
| Y: 0 – 27120 | Y: 0 – 2712 |

変換式: `論理座標 = getevent値 // 10`

### テストスクリプト

`tests/test_tap_detect.py` — 人間がタップした座標をターミナルに表示する。

```
python tests/test_tap_detect.py
```

タップした瞬間（指が離れたタイミング）に座標を出力する。Ctrl+C で終了。

---

## 注意事項

- `--target home` は `keyevent 3`（HOME ボタン）を送信するため、フォアグラウンドのアプリ（Firefox 等）がバックグラウンドに移動する。現在の画面をそのまま操作したい場合は `--target current` を使う。
- scrcpy サーバーは `cleanup=false` で起動するが、`kill_existing_servers()` で `pkill -f "scid=2"` により前回起動分のみ終了する。他の scrcpy セッションには影響しない。
- `input()` による Enter 待ちはターミナルから直接実行する必要がある（Claude Code から `!` プレフィックスで実行）。
