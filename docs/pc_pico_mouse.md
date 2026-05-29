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

### `click_at()` の短距離 detour 経由化

短距離（距離 ≤ `_SHORT_MOVE_THRESHOLD_PX = 80 px`）の click_at は、直接 target へ向かう代わりに **必ず長距離アプローチを踏ませる**。target から `_SHORT_MOVE_DETOUR_OFFSET_PX = 300 px` 離れた detour 位置へ一旦ジャンプし、そこから target へ `move_to_accurate` で戻す方式。

**なぜ detour が必要か:**

- 直接の短距離 HID 相対移動だと、リモート無しのローカル単独運用でも視覚的にクリック位置が下にズレる事象が出ていた
- GetCursorPos ベースの誤差は ±2-3 px に収まっているのに着弾点がズレるため、原因は HID 加速の建ち上がり不足 / 終端の沈み込みと推定（短距離だと OS のポインター加速が機能する前に動作が終わる）
- detour 経由なら必ず「長距離アプローチ」になり、`move_to_accurate` の ease-out + 補正ループが効く環境にカーソルを持ち込める

**実装パラメータ:**

| 定数 | 値 | 意味 |
|---|---|---|
| `_SHORT_MOVE_THRESHOLD_PX` | 80 | この距離以下は detour 経由 |
| `_SHORT_MOVE_DETOUR_OFFSET_PX` | 300 | target から detour 位置までの距離 |
| インスタンス属性 `short_move_detour` | `True`（既定） | 切戻し用フラグ。`False` で旧挙動（短距離直行 step=5, delay=0.04） |

detour 位置は target 座標が `offset` より大きい側からは引き、小さい側からは加えることで画面端を避ける:

```
detour_x = x - 300 if x > 300 else x + 300
detour_y = y - 300 if y > 300 else y + 300
```

**コスト:** 1 タップあたり ~500-700ms 増。9 タップのポーション補給シーンで約 5 秒増。実機検証 (2026-05-29) で短距離タップのズレが解消したのを確認。

**運用方針:** 既定 ON 維持。劣化が出たら閾値や offset の微調整で対応し、`short_move_detour = False` への切戻しは最終手段。詳細経緯は `docs/changelog.md` の 2026-05-29 エントリ参照。

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

## ドラッグ操作の戦略（ハイブリッド方式）

PC GUI のテストタブで検証して確定した、Nightcrows で安定動作するドラッグ手順。

### 動作シーケンス

| 工程 | 内容 | 実装 |
|------|------|------|
| 1 | **開始位置へ移動** | 「滑らか」モード: `move_to_accurate(step=max_step, delay=delay)` で誤差補正付き／「ジャンプ」モード: `move_cursor(sx, sy)` で SetCursorPos 瞬間移動 |
| 2 | **一呼吸** | `time.sleep(0.2)` （`_DRAG_PAUSE_MS = 200`） |
| 3 | **左ボタン押下** | `press("L")` |
| 4 | **終了位置へ移動** | **必ず滑らか移動** `move_to(max_step=8, delay=0.05)` ≒ 160 px/s。イーズアウトで終端は更に減速 |
| 5 | **左ボタン解除** | `release("L")` （例外時も `finally` で確実に） |

### なぜドラッグ移動 (工程 4) は滑らかでないとダメか

ゲームは **Raw Input でマウス移動イベントを読んでドラッグを判定する** ことが多い。`SetCursorPos` はカーソル位置を変えるが Raw Input にマウス移動イベントを発火しないので、ゲームから見ると「動いていない」状態になりドラッグとして認識されない。

→ `press` → `release` が同位置で起きたかのように見え、**ドラッグではなく単発クリック扱い** になる。

これに対し HID 相対移動（Pico の `MOVE` コマンド）は物理マウス入力として Raw Input にも届くので、ゲームが軌跡を観測でき、正しくドラッグと認識される。

### 開始位置はジャンプでも OK

開始位置への移動はマウス動きを必要としないため、`SetCursorPos` での瞬間移動でも問題ない。  
**TeamViewer などのリモートツールも開始位置移動を瞬間ジャンプで送る** が、Nightcrows ではマクロ判定されない。これは Pico HID も同様（OS から見ればどちらも物理マウス入力カテゴリ）。

### パラメータ定数（`pc/gui/pc_main.py`）

| 定数 | 値 | 意味 |
|------|----|------|
| `_DRAG_MAX_STEP` | 8 | ドラッグ中の 1 イベントあたり最大移動量 (px) |
| `_DRAG_DELAY` | 0.05 | ドラッグ中のイベント間隔 (s) → 約 160 px/s |
| `_DRAG_PAUSE_MS` | 200 | 開始位置到達後・press 直前の一呼吸 (ms) |

開始位置移動は「滑らか」モードでは速度スライダーに従い、「ジャンプ」モードでは速度無関係。ドラッグ本体（工程 4）は **モードによらず固定 160 px/s**。

---

## 今後の拡張ポイント

- **速度プロファイル**: `move_to()` の `max_step` / `delay` で調整可能
- **シーン定義**: クリック・移動・スイープを組み合わせてシーン化する際は `pico_mouse.py` の API を直接使用する
- **キーボード**: Pico ファームウェアに `KEY` / `KEY_RELEASE` コマンドを追加すれば `adafruit_hid.keyboard` で対応可能
