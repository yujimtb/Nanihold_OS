# Tasks: improve-audit-trace-api

> 本 change は設計起草のみ。実装しない。以下は設計提示項目と、実装 change 起票に向けた準備項目。要件は 2 つの WorkItem に対応して 2 群(並列安全化 / 性能・正しさ・非破壊)に分ける。

## Track A. 設計提示

- [x] A1 並列障害(2 件並行 → 両方無応答)の作業仮説を design.md に記述する(同期ハンドラ + 全走査の二重問題、切断非キャンセル)
  - Spec: ATA-01, ATA-02
- [x] A2 クライアント切断後のサーバ側処理有界化(切断検知・協調キャンセル・並行度上限)を記述する
  - Spec: ATA-02, ATA-01
- [x] A3 性能ボトルネック(`_events()` の cursor 0 全走査、`OperationalLedger` に相関/type 絞り込みが無い)を根拠付きで記述する
  - Spec: ATA-03
- [x] A4 LETHE 依存の有無で 2 設計(案 A: LETHE 相関/type/keyset 索引前提、案 B: 依存なし・stream()+ローカル派生索引)を比較し Decision を明記する
  - Spec: ATA-03
- [x] A5 改善前後一致(canonical 全走査との等価回帰)と read-only 原則・派生索引の replay 可能性を記述する
  - Spec: ATA-04
- [x] A6 wire 契約・公開インターフェース・既存テストの非破壊条件を記述する
  - Spec: ATA-05

## Track B. LETHE 側依存の扱い(提案の固め)

- [x] B1 LETHE 側 `/api/operational-events` 相関/type/keyset 索引を **soft 依存(待たない)** とし、案 B を主経路にする方針を確定する
  - Spec: ATA-03
- [ ] B2 skcollege_database 側の当該読み取り索引 change の正式名称・スコープをオーナー / LETHE 側と突き合わせ、fast-path(案 A)差し替え契約を確定する(未決)
  - Spec: ATA-03

## Track C. 検収

- [x] C1 `openspec validate improve-audit-trace-api --strict` を通す
- [ ] C2 本設計を反映した実装 change を別途起票する(本 change は実装しない)

## 実装 WorkItem 分割案(参考 / 本 change では実装しない)

1. `work:audit-trace-performance` 主対応: `_events()` 全走査を stream() ベースの対象取得へ置換(案 B1)。ATA-03 / ATA-04。
2. `work:audit-trace-concurrency` 主対応: 該当 2 ルートを `async def` 化 + 有界オフロード + 切断協調キャンセル + 並行度上限。ATA-01 / ATA-02。
3. 等価回帰ハーネス(canonical 全走査 vs 新実装のバイト等価)と 5 万イベント合成台帳の性能/並列/切断ベンチ。ATA-03 / ATA-04。
4. (任意・LETHE 側 landing 後)案 A fast-path: `OperationalLedger` 絞り込み読みと `InMemoryOperationalLedger` 実装、B を fallback に残す。ATA-03。
