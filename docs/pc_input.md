# PC 自動入力 検証メモ

PC 版 Nightcrows のボタンを Python から自動操作できるかを検証した記録（2026-05-22）。
モバイル版 (Android) の `touch_input.md` 相当を PC 環境で実現できるかを試した。

---

## 結論（先に）

| 方式 | Nightcrows での反応 | 備考 |
|---|---|---|
| `SendInput` マウスクリック | **無反応** ✗ | カーソルは動くがクリック判定なし。`LLMHF_INJECTED` を見たチート対策で弾かれている |
| `InjectTouchInput` (Touch Injection API) | **未検証** | リモート(TeamViewer)経由では `ERROR_INVALID_PARAMETER (87)` でテスト不可 |
| TeamViewer 経由の人手マウス | ✓ 反応 | ミラードライバが物理入力として注入するため通る |
| 物理マウス | ✓ 反応（想定） | — |

→ **合成入力は弾かれる**。物理入力相当でないと動かない。

---

## 検証した方式と詳細

### 方式 A: `SendInput` でマウスクリック

検証スクリプト: `pc/click_test.py`

- `SetCursorPos` で対象座標へカーソル移動 → `SendInput(MOUSEEVENTF_LEFTDOWN/UP)`
- **メモ帳・ブラウザでは正常動作**
- **Nightcrows ではカーソルだけ動いてクリック無反応**
- 原因: Wemade 系ゲームは Raw Input でマウスを受け、`LLMHF_INJECTED` フラグが立った入力は無視する典型的なチート対策

### 方式 B: `InjectTouchInput` (Touch Injection API)

検証スクリプト: `pc/tap_test.py`

- `user32.dll` の `InitializeTouchInjection` + `InjectTouchInput` を ctypes で呼び出し
- タッチハードウェアが無くても仮想タッチを注入できる仕様
- DPI Awareness を Per-Monitor V2 に設定、構造体サイズ・座標範囲も検証済み（サイズ 96/144、座標は仮想スクリーン内）

#### Error 87 (ERROR_INVALID_PARAMETER) で失敗

```
sizeof(POINTER_INFO)       = 96  (OK)
sizeof(POINTER_TOUCH_INFO) = 144 (OK)
仮想スクリーン: (0,0)-(1919,1079)
タップ実行: (1101, 640)
→ InjectTouchInput(down) failed: error=87
```

試した修正で解消しなかったもの:
- `historyCount = 1` / `dwTime = GetTickCount()` の明示
- 全フラグを `c_uint32` に修正
- `TOUCH_FEEDBACK_NONE` への変更
- `dwTime` / `historyCount` を Microsoft C++ サンプルと同じく 0 に戻す

#### TeamViewer 経由が原因の可能性が濃厚

`InjectTouchInput` は仕様上「呼び出しアプリのセッションのデスクトップウィンドウ」に対して動作する。TeamViewer の入力レイヤー / ミラードライバが介在すると「対話的セッションではない」と判定され 87 を返すケースがあるとの報告。

**ローカル(コンソール)で実行すれば通る可能性が残っているが、現在ローカルアクセスができないため未確定**。

---

## TeamViewer 経由の特殊性

| 入力経路 | Nightcrows | なぜ |
|---|---|---|
| TeamViewer のマウス操作 | ✓ 通る | TeamViewer は専用ドライバ経由で「物理マウス入力」として注入する → Raw Input にも本物として届き、`LLMHF_INJECTED` も立たない |
| Python の `SendInput` | ✗ 通らない | 合成入力フラグが立つ |
| Python の `InjectTouchInput` | テスト不可 | リモートセッション制限で API 自体が拒否 |

つまり「TeamViewer で動かせる ≠ SendInput で動かせる」。物理マウスと TeamViewer は同じカテゴリ、`SendInput` だけ別カテゴリ。

---

## 残っている選択肢

| 案 | 内容 | 実現性 | リスク |
|---|---|---|---|
| **Interception ドライバ** ([oblitum/Interception](https://github.com/oblitum/Interception)) | 署名付き keyboard/mouse class driver。物理入力と区別不能な入力を Python から送れる | リモートでも導入可能、現実的 | **規約違反・BAN リスク** |
| **Touch Injection をローカルで再検証** | ホストPCに物理アクセスして tap_test.py を実行。WM_POINTER 経由なので Raw Input フィルタを回避できる可能性 | ローカルアクセス次第 | Touch も検知される実装ならダメ |
| **Arduino / USB HID デバイス** | 物理ハードウェアで HID 入力を生成 | 確実だがハードウェア必要 | 物理デバイス管理が手間 |
| **mobile/ の adb 版を継続利用** | モバイル版 Nightcrows を adb で操作（既存実装） | 既に動いている | PC版に切り替える話自体が成立しない |

---

## 注意

Nightcrows を含む多くの MMO は **マクロ・Bot による自動操作を利用規約で禁止** している。Interception 等のドライバを使った自動入力はチート対策を技術的に回避することになり、検知されれば BAN 対象になり得る。自己責任で判断すること。

---

## 関連ファイル

- `pc/tap_test.py` — Touch Injection 検証用（Error 87 で未成功）
- `pc/click_test.py` — SendInput 検証用（合成入力は弾かれることを確認済み）
- `docs/touch_input.md` — モバイル版 (Android/adb) のタッチ実装まとめ
