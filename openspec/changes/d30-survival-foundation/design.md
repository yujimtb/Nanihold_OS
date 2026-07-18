# D30 Survival Wave 0〜2 実装結果設計

## 境界

既存 `BudgetLedger` はトークンと wall clock の実行上限として維持する。円建て会計は
`vsm.survival` の `EconomicLedger` / `UsageRecord` / `PriceBook` / `DailyReportGenerator`
へ分離する。実行ランタイムには optional な `metering_hook` だけを追加し、budget/quota/
messaging/cancel の実装は変更しない。

## Wave 0

- `verify_baseline(RunConfig())` で固定基線（S2/S3/S3*/S4/S5 は各1、起動時S1は0、S1上限1024、動的上限64）を検証する。
- `SafetyBoundary` は loopback bind、外部送信・実請求無効、Human 認証 placeholder を保持する。

## Wave 1

`LedgerEntry` の append-only JSONL と `UsageRecord` の JSONL は再起動後に再読込する。
Price Profile と FX は TOML から読み込み、未登録・期間重複・為替欠落は fail-fast である。
runtime metering は価格化できた usage だけを負の円 `expense` entry にし、未価格化は明示状態で残す。

## Wave 2

日付単位の report snapshot は同じ日付で再生成せず、30日推移、cash/economic burn、runway、
R、owner dependency、unpriced usage を返す。FastAPI の `/api/survival/dashboard` と React の
「事業状況」タブは既存の技術 dashboard から分離する。
