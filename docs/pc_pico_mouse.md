# PC Pico HID マウス設計書

## 概要

Raspberry Pi Pico を USB HID マウスデバイスとして動作させ、PC Python から制御する仕組み。
`SendInput` は Nightcrows のチート検知（`LLMHF_INJECTED` フラグ）で弾かれるが、
Pico 経由の HID 入力はカーネルドライバレベルで物理マウスと区別不能なため通過する。

```
PC Python ──シリアル(USB CDC)──▶ Pico ──USB HID──▶ Windows
  コマンド送信                    物理クリック/移動
```

---

## ファイル構成

| ファイル | 役割 |
|---|---|
| `pc/pico/code.py` | Pico 上で動く CircuitPython ファームウェア |
| `pc/pico_mouse.py` | PC 側コントローラクラス `PicoMouse` |
| `pc/move_test.py` | 動作確認・精度検証ツール（対話メニュー形式） |
| `pc/pico_ping.py` | シリアルポート疎通確認ツール |
| `pc/pico_test.py` | 最小クリックテスト |
| `pc/scene_test.py` | シーン動作テスト（旧版、現在は move_test の 8 を使用） |

---

## Pico ファームウェア (`pc/pico/code.py`)

### セットアップ

1. [circuitpython.org](https://circuitpython.org/board/raspberry_pi_pico/) から CircuitPython `.uf2` を書き込む
2. `adafruit_hid` ライブラリを `CIRCUITPY/lib/` に配置
3. `pc/pico/code.py` を `CIRCUITPY/code.py` にコピー
4. `CIRCUITPY/boot.py` で `usb_cdc.enable(console=True, data=True)` を設定

### USB ポート構成

CircuitPython は CDC ポートを 2 つ作る。

| COM番号（例） | 種別 | 用途 |
|---|---|---|
| COM4 | `usb_cdc.data` | 未使用 |
| COM5 | console (`sys.stdin/stdout`) | コマンド通信に使用 |

番号が大きい方が console ポート。`PicoMouse` は自動で後者を選択する。

### コマンドプロトコル

テキスト行（`\n` 区切り）、レスポンスも 1 行（`OK` または `ERROR ...`）。

| コマンド | 書式 | 動作 |
|---|---|---|
| PING | `PING` | 疎通確認 |
| HOLD | `HOLD L\|R\|M` | ボタン押し続ける |
| RELEASE | `RELEASE [L\|R\|M]` | ボタンを離す（省略で全解放） |
| CLICK | `CLICK L\|R\|M [hold_ms]` | クリック（デフォルト 30ms） |
| MOVE | `MOVE dx dy` | 相対移動（各 -127〜127） |

---

## PC コントローラ (`pc/pico_mouse.py`)

### クラス `PicoMouse`

```python
mouse = PicoMouse()          # 自動ポート検出
mouse = PicoMouse("COM5")    # ポート指定
```

### 初期化フロー

```
Serial open → 2秒待機（CircuitPython 再起動待ち）→ reset_input_buffer → PING 疎通確認
```

### メソッド一覧

| メソッド | 説明 |
|---|---|
| `ping()` | 疎通確認。失敗で RuntimeError |
| `calibrate(test_hid=60)` | ポインター速度スケールを測定して `_speed_scale` に保存 |
| `get_cursor_pos()` | `GetCursorPos` で現在カーソル座標を取得 |
| `move_cursor(x, y)` | `SetCursorPos` でカーソルを瞬間移動（HID イベントなし） |
| `click(x, y, button, hold_ms)` | `move_cursor` + Pico CLICK |
| `press(button)` | Pico HOLD（ボタン押し続け） |
| `release(button="")` | Pico RELEASE（省略で全解放） |
| `move(dx, dy)` | Pico MOVE（生 HID 相対移動） |
| `move_to(x, y, ...)` | イーズアウト付き絶対座標移動 |
| `move_to_accurate(x, y, ...)` | `move_to` + フィードバック補正ループ |

### キャリブレーション

ポインター速度（コントロールパネルのマウス設定）が 1:1 でない場合、
HID 1 単位 ≠ 1 画素になる。`calibrate()` で実際の比率を測定する。

```python
scale = mouse.calibrate()
# HID 60 単位送信 → 実移動 102px なら scale = 1.7
```

測定後は `move_to()` 内で自動的に HID 単位へ変換する。

### `move_to()` のアルゴリズム（イーズアウト）

```
残距離 dist = max(|dx|, |dy|)
ステップ = clamp(dist // 3, min_step, max_step)
HID単位 = int(ステップ / speed_scale)
```

- 遠いとき: dist=900 → step=20（最大値） → 高速移動
- 近づくと: dist=30 → step=10、dist=9 → step=3 と自動減速
- 精度: 最終ステップが `min_step=1px` → 残差は `speed_scale/2` px 以下

### `move_to_accurate()` のアルゴリズム

```
1. move_to() で初回移動
2. GetCursorPos で実位置を測定
3. 残差が tolerance 以内 → 終了
4. 残差に応じた小ステップで補正 move_to() → 2 へ戻る（最大 max_iter 回）
```

キャリブレーション済みであれば通常 1〜3 回で収束する。

### 絶対座標クリックについて

```
SetCursorPos(x, y)   ← カーソル位置をメモリ上で書き換え（HID イベントなし）
Pico CLICK           ← 物理クリックイベント（LLMHF_INJECTED フラグなし）
```

`WM_LBUTTONDOWN` にはクリック時点のカーソル座標が付与されるため、
Pico 側でカーソルを動かさなくても正確な絶対座標クリックが実現できる。

---

## move_test.py メニュー

| 選択肢 | 機能 |
|---|---|
| `c` | 再キャリブレーション |
| `1` | 画面中央へ移動（精度確認） |
| `2` | 四隅を順番に巡回 |
| `3` | 座標指定移動（誤差自動補正あり） |
| `4` | 現在カーソル位置をクリック |
| `5` | 四隅巡回（誤差自動補正あり） |
| `6` | クリック座標取得（左クリック=記録、右クリック=終了） |
| `7` | 精度限界テスト（同一座標へ繰り返し移動して誤差統計） |
| `8` | シーンテスト（クリック → 左右スイープ） |

### 選択肢 8 のシーン動作

```
[1] 指定座標を左クリック
[2] スイープ中心へ SetCursorPos で移動
[3] 指定ボタン（R/L/M）押し下げ
[4] 左端へ move_to()
[5] 中央へ move_to()
[6] 右端へ move_to()
[7] 中央へ move_to()
[8] ボタン解放
```

---

## 精度特性

| 条件 | 到達精度 | 補正回数 |
|---|---|---|
| キャリブレーション済み | ±1〜3px | 1〜3回 |
| キャリブレーションなし（scale=1.0固定） | ±数十px〜 | 8回で未収束も |
| ポインター加速オフ・速度6/11 | ±1px | 0〜1回 |

理論最小誤差 = `speed_scale / 2` px（HID が整数値のみのため）。

---

## 今後の拡張ポイント

- **ドラッグ操作**: `press()` → `move_to()` → `release()` の組み合わせで実現済み
- **速度プロファイル**: `move_to()` の `max_step` / `delay` で調整可能
- **シーン定義**: クリック・移動・スイープを組み合わせてシーン化する際は `pico_mouse.py` の API を直接使用する
- **キーボード**: Pico ファームウェアに `KEY` / `KEY_RELEASE` コマンドを追加すれば `adafruit_hid.keyboard` で対応可能
