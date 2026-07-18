# Economic ledger / Survival report

Wave 0〜2では、既存の `[budget]`（実行を止める上限）と、実際の原価・資金を測る
`vsm.survival` を分離している。

## 保存と入力

- `runs/survival/ledger.jsonl`: 円建ての append-only cash ledger
- `runs/survival/usage.jsonl`: invocation/run/node 別の利用量。価格未確定も削除せず `unpriced` で保持
- `runs/survival/reports.jsonl`: `survival:YYYY-MM-DD` 相当の日付 idempotency を持つ日次 snapshot
- `vsm.survival.events.ECONOMICS_PAYLOAD_MODELS`: schema version 1 の経済イベント registry
- `config/survival-pricing.toml`: オーナーが確定した Price Profile と FX。雛形は `config/survival-pricing.toml.example`

金額は円の整数、外貨・単価・為替は Decimal 文字列表現で保存する。価格・為替が解決できない
usage は別モデルへ黙って流さず、dashboard の `unpriced_usage` に出す。ledger の訂正は既存行を
書き換えず、反対仕訳を `reverses_entry_id` 付きで追加する。

売上は `kind = revenue`、支出は負の `signed_amount_jpy` を持つ `kind = expense`、オーナー拠出は
`kind = owner_contribution` として手入力 API (`POST /api/survival/ledger`) から登録する。
これは税務帳簿や実請求ではなく、経営・原価判断の operational ledger である。

## 円換算

実行日の完全一致 `provider + billing_mode + model` の Price Profile と、同日の `USD/JPY` 等の
FX を要求する。入力、出力、cache read/write、tool unit、wall clock を個別単価で計算し、最終的な円額を
ROUND_HALF_UP で整数化する。Price Profile と FX の欠落、期間重複は `CostingError` とする。

## 日次指標

`GET /api/survival/dashboard` が日次 snapshot と30日推移を返す。

- `available_cash`: その日までの signed ledger 合計
- `burn_30d_cash` / `burn_30d_economic`: 直近30日の支出。分母0時は runway/R を null にし reason を付ける
- `runway_months`（互換表示用に `runway_days` も出力）: available cash ÷ 30日 burn
- `R_cash` / `R_economic`: 期間売上 ÷ 期間支出
- `owner_dependency`: オーナー拠出と `owner_paid*` 費目
- `unpriced_usage`: 価格・為替の未解決件数と token 数

本 Wave では外部送信・実請求を実装しない。Human 認証は `TODO_OWNER_APPROVAL` の設定表示のみで、
認証済みとは扱わない。
