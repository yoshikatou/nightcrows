# ステップ型リファレンス

シーン JSON の `steps` 配列に入るステップの全型と JSON 構造。

---

## 基本操作

### `tap`

指定座標をタップする（`input swipe` で同座標 swipe を実行）。

```json
{ "type": "tap", "x": 700, "y": 1146, "duration_ms": 100 }
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `x`, `y` | int | 論理座標（画面回転変換済み） |
| `duration_ms` | int | 押し続ける時間（ms）。100 = 短タップ |

---

### `swipe`

2点間をスワイプする。

```json
{ "type": "swipe", "x1": 610, "y1": 1200, "x2": 610, "y2": 600, "duration_ms": 500 }
```

---

### `scroll`

ジッターつきスクロール。ランダム幅でばらつかせて bot 検知を回避する。

```json
{
  "type": "scroll",
  "x1": 600, "y1": 1400, "x2": 600, "y2": 600,
  "x1_jitter": 20, "y1_jitter": 10,
  "x2_jitter": 20, "y2_jitter": 10,
  "duration_ms": 800, "duration_jitter_ms": 200
}
```

各座標に `[-jitter, +jitter]` の乱数を加算してから実行する。

---

### `keyevent`

Android キーイベントを送信する。

```json
{ "type": "keyevent", "keycode": "KEYCODE_BACK" }
```

よく使うコード: `KEYCODE_BACK` / `KEYCODE_HOME` / `KEYCODE_APP_SWITCH` / `KEYCODE_ENTER`

---

## 待機

### `wait_fixed`

固定時間待機（割り込み可能: 100ms 単位でストップフラグを確認）。

```json
{ "type": "wait_fixed", "seconds": 3.0 }
```

---

### `wait_image`

テンプレート画像が画面に現れるまで待機する。タイムアウトで中断。

```json
{
  "type": "wait_image",
  "template": "templates/snapshots/loading_done.png",
  "region": [0, 0, 1220, 400],
  "threshold": 0.85,
  "timeout_s": 30
}
```

| フィールド | デフォルト | 説明 |
|-----------|-----------|------|
| `template` | — | テンプレート画像パス |
| `region` | null（全画面） | `[x, y, w, h]` 検索領域 |
| `threshold` | 0.85 | マッチスコア閾値（0〜1） |
| `timeout_s` | 30 | タイムアウト秒数 |

---

## 画像マッチング

### `tap_image`

テンプレートが画面に現れたらその中心をタップして進む。

```json
{
  "type": "tap_image",
  "template": "templates/btn_ok.png",
  "region": [400, 800, 400, 200],
  "threshold": 0.85,
  "timeout_s": 20,
  "tap_offset_x": 0,
  "tap_offset_y": 0
}
```

`tap_offset_x/y` でテンプレート中心からオフセットしてタップできる。

---

### `if_image`

テンプレートマッチの結果で `then` / `else` のどちらかを実行する。

```json
{
  "type": "if_image",
  "template": "templates/dialog_confirm.png",
  "region": [300, 600, 600, 400],
  "threshold": 0.85,
  "then_steps": [
    { "type": "tap", "x": 610, "y": 900 }
  ],
  "else_steps": []
}
```

- `then_steps` / `else_steps`: インラインステップ配列（ネストした `Step` のリスト）
- 旧形式 `then_scene` / `else_scene`（シーンパス文字列）も後方互換で読み込み可

---

## シーン呼び出し

### `call_scene`

別のシーンファイルをサブシーンとして呼び出す。最大深度 10。

```json
{ "type": "call_scene", "scene": "scenes/common/open_menu.json" }
```

---

### `pick_scene`

シーンリストから1つを選んで実行する。ランダムまたは順番ローテーション。

```json
{
  "type": "pick_scene",
  "mode": "sequential",
  "scenes": [
    "scenes/maps/map_a.json",
    "scenes/maps/map_b.json",
    "scenes/maps/map_c.json"
  ],
  "step_id": "abc12345"
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `mode` | `"random"` \| `"sequential"` | 選択方式 |
| `scenes` | string[] | 実行候補のシーンパスリスト |
| `step_id` | string | 順番カウンタを識別するための UUID（自動生成） |

**`sequential` モードのカウンタ持続:**

- フロー実行開始から停止まで `_seq_state: dict[str, int]` で保持
- `step_id` をキーとするため、同一シーン内の複数 `pick_scene` は独立して管理
- 停止→再開でリセット（新しい辞書が作られるため）

**ログ出力例:**
```
[3/10] pick_scene {'mode': 'sequential', ...}
  pick_scene [順番 2/3]: scenes/maps/map_b.json
  → [マップB]
```

---

## 記録・グループ

### `snapshot`

スナップショット画像を記録するためのマーカー。実行時はスキップ。

```json
{ "type": "snapshot", "path": "templates/snapshots/snap_20260424_120000.png" }
```

---

### `group_header`

複数ステップをグループとしてまとめるラベル。実行時はスキップ。シーン編集画面で青い見出しとして表示。

```json
{ "type": "group_header", "label": "ポーション購入" }
```

---

## _seq_state の伝播経路

`sequential` モードのカウンタは以下の全呼び出し経路で同一辞書を共有する：

```
replay_flow
  └─ run_scene(path)
       └─ replay_scene(scene, _seq_state=seq_state)
            ├─ pick_scene  → _do_pick_scene(..., seq_state)
            ├─ call_scene  → _do_call_scene(..., seq_state)
            │    └─ replay_scene(_seq_state=seq_state)
            └─ if_image    → _do_if_image(..., seq_state)
                 └─ replay_scene(_seq_state=seq_state)
```
