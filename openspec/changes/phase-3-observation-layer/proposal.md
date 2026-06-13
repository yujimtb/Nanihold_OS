# Phase 3: 観測層統合(8月 〜 9月、Phase 2 後半と並行)

## Why

ここがプロダクトの中核 — LETHE を汎用データ基盤に拡張し、Nanihold 側で組織構造を
ファジィに推論する。9月末に「**観測 → 構造表示 → 人間修正**」のループが実データで回る状態を作る。
O3 推論の品質はデータ蓄積のカレンダー時間に依存するため、**セルフ観測の開始日(8月中)が
2027/3 の製品品質の実質的決定要因**になる。

## What Changes

**LETHE 側(主担当: 友人)**
- `lethe-tenant`(L-4): org/tenant 抽象(SQLite ファイル分離)
- `lethe-schema-config`(L-5): 寮固有スキーマの設定化
- `lethe-discord-adapter`(L-6): Discord adapter(自社セルフ観測 + β先)
- `lethe-ingestion-gate`(L-7): IngestionGate の consent policy 接続
- `lethe-retention`(L-8): retention / 削除 API
- `lethe-write-back`(L-9): Command/EffectPlan → ToolInvocation 変換層

**Nanihold 側(主担当: 本人)**
- `observed-topology`(N-20): ObservedTopology projection(役割は重み分布)
- `structure-inference`(N-21): O3 構造推論バッチ
- `human-correction-loop`(N-22): O5 人間確認ループ UI
- `automation-levels`(N-23): O6 自動化レベル(Lv0〜Lv3)

## Impact

- Affected specs (new): 上記10 capability
- Affected code: LETHE(tenant・adapter・gate・retention・write-back)、Nanihold(projection・
  推論バッチ・確認 UI・自動化レベル)
- 依存: N-20←N-12/N-16 / N-21←N-20+scheduler / N-22←N-20 / N-23←N-9+N-10 /
  L-9←N-10+N-16
- セルフ観測開始(8月中・最重要): 自社 Discord/Slack を取り込み、N-20/21 を自社相手に常時運用
- マイルストーン: **9/30 自社 + SHIMOKITA 実データで観測→構造表示→修正ループが回り、HumanCorrection が蓄積開始**
