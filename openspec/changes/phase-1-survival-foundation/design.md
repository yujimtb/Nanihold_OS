# Design — Phase 1: 生存基盤

## Context

Phase 0 の収入を背景に、Nanihold ランタイムへ自律実行を任せられる状態にする。
判断基準は「動いた」ではなく「壊し方を試して耐えた」。7/12 の障害演習6シナリオが
本フェーズの最終ゲート。

## Goals / Non-Goals

**Goals**
- 暴走・クラッシュ・スタックが構造的に止まる/復旧する/通知されるランタイム
- 受託の会計・ライフサイクルの最小配管(org_id 基盤を含む)

**Non-Goals**
- persistent サービス Node / lethe_query(Phase 2)
- 観測層・構造推論(Phase 3)
- マルチテナント実運用(Phase 4)

## Decisions

- **append-only スキーマは追加のみ**: EventEnvelope への org_id/customer_id/provenance は
  「optional フィールド追加 + デフォルト値」で行い、既存 events.jsonl の replay 互換を壊さない
  (replay 時は `org_self` 補完)。破壊的変更は禁止。
- **N-12 を Phase 1 で必ず入れる**: org_id はすべてのテナント系機能の祖先。後送りすると
  replay 互換の負債が膨らむため、Week C で確実に投入する。
- **キャップ判定は二段**: ToolInvocation 発行前(事前見積)と CostRecorded 時(事後確定)。
  **自動でキャップを引き上げる経路は作らない**(人間のみが引き上げられる)。
- **algedonic は階層バイパス**: 緊急通知は S1→S2→S3 を経由せず、EscalationFacade から
  直接 Discord adapter へ。通知失敗が通知を呼ぶループを構造的に避ける。
- **idempotency を復旧の中核に置く**: kill -9 後の再起動で実行中だった Run は「中断」記録とし、
  idempotency により二重実行しない。スケジューラ発火も schedule_id+予定時刻で冪等化。
- **ParentAuthority による制御権限**: lifecycle 制御は親系列のみ。横・下からの制御は拒否。
- **防御はデータ層/scope に置く**: 顧客入力は「データ」としてのみ扱い、CONTROL/EXTERNAL_WRITE の
  引数に直接展開しない。唯一の防壁は CodexRunPolicy の scope→sandbox 変換であり、テストで担保。
- **請求の自動化はしない**: 銀行 API 接続は当面リスク対効果が合わない。手動 CLI 記録に留め、
  請求漏れは日次レポートで構造的に検出する。

## 追加イベント型(append-only、§14.1)

| イベント | 主フィールド |
|---|---|
| CostRecorded | run_id, node_id, org_id, customer_id?, model, tokens, jpy, rate |
| BudgetCapTripped | scope(daily/monthly), org_id, action(suspend/reject) |
| KillSwitchActivated / Released | actor, reason |
| ScheduleFired | schedule_id, planned_at, fired_at |
| WatchdogTripped | node_id, idle_seconds, action |
| ReviewRequested / Resolved / Expired | review_id, subject_ref, decision, actor |
| LeadRegistered / EstimateIssued / OrderAccepted / DeliverySubmitted / AcceptanceRecorded / InvoiceIssued / PaymentReceived | customer_id, amount_jpy, refs |

## Risks / Trade-offs

- **R3 Phase 1 遅延**(検知: 7/12 演習未通過)→ 営業開始を8月末まで後退可(2027/3 に影響なし、バッファ2ヶ月)
- **R6 コスト暴走**(検知: N-2/N-5)→ ハードキャップで構造的防止。チャットボットサブスクが下支え
- **R7 LLM プロバイダ全断**(検知: health/エラー率)→ LiteLLM fallback 2系統。演習シナリオ1で検証

## 障害演習6シナリオ(7/12 の最終ゲート)

1. LLM プロバイダ全断 → fallback or 安全停止
2. 予算枯渇(キャップ 100円)→ N-2
3. Run ハング → N-8
4. プロセス kill -9 → N-4
5. kill switch → N-3
6. Discord 不達 → 通知失敗の検出手段

全6シナリオで想定挙動 = 営業可能状態。結果記録が運用 Runbook の初版になる。
