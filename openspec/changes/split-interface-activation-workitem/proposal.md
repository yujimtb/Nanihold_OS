# Change Proposal: split-interface-activation-workitem

**Version:** 1.0
**Date:** 2026-07-20
**Status:** Proposed
**Repository:** Nanihold_OS
**Type:** 巨大 WorkItem の廃棄と小粒 WorkItem 群への置換
**Source:** オーナー決定(2026-07-20 Q2)

---

## Why

WorkItem `work:interface-effective-activity-start` は単一項目としては巨大すぎ、`token_budget = 12k` / `300` 秒の実行制限では完了不能である。初回実行は 300 秒の ProviderTimeout で停止し、最初のモデル呼び出しだけで入力 14,349 トークンに達した(制限 12k を単一呼び出しで超過)。実行系の budget 検査は turn 完了後にのみ行われ、turn の途中で停止しないため、超過した呼び出しがそのまま走り切ってしまう構造も判明した。

オーナー決定(2026-07-20 Q2)により、この巨大 WorkItem を廃棄し、既達条件を除いた小粒 WorkItem 群へ置換する。既達条件は「ReorientationAssessment 提示済み」および「owner 承認済み ACTIVE」であり、これらは再実行の対象から外す。制約として `token_budget = 12k` / `300` 秒の制限値は維持する(緩めない)。

## What Changes

- **REMOVED:** WorkItem `work:interface-effective-activity-start`(巨大・完了不能)を廃棄する。
- **ADDED:** 既達条件(ReorientationAssessment 提示済み・owner 承認済み ACTIVE)を除いた残作業を、`1 WorkItem = 12k トークン / 300 秒で完了可能な粒度` に縛った小粒 WorkItem 群へ棚卸し・起票する。
- **ADDED:** 分割リストの骨子を提案として記述する。最終の分割リストはオーナー承認事項とする。
- **MODIFIED:** なし(budget 制限値 12k/300 秒は維持し、変更しない)。

## Non-Goals

- `token_budget` 制限値の緩和(維持する)。
- 実行系の budget 検査タイミング(turn 途中停止の有無)の実装変更。これは診断事実として背景に記すが、本 change のスコープは WorkItem の分割であり、実行系改修は別 change とする。
- 実行 sandbox の read-only 問題の恒久修正(Codex Desktop 側 managed permission の確認は前提事項として記すが、修正実装は別途)。
- 分割後 WorkItem の実装そのもの。

## Affected Invariants

`token_budget = 12k` / `300` 秒の制限を維持する。既達条件(ReorientationAssessment 提示済み・owner 承認済み ACTIVE)は再実行しない。

## Rollout

分割リストはオーナー承認を経て確定する。承認後、旧 WorkItem を廃棄し小粒 WorkItem 群を起票する。
