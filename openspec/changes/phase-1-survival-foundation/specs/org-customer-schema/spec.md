# org-customer-schema(N-12)

## ADDED Requirements

### Requirement: EventEnvelope への org/customer/provenance 追加

システムは、EventEnvelope に org_id(必須)・customer_id(任意)・provenance を、append-only 互換の「フィールド追加 + デフォルト値」でのみ追加しなければならない (SHALL)。

#### Scenario: 既存ログが replay 互換である
- **WHEN** 既存の events.jsonl を全件 replay する
- **THEN** org_id="org_self" が補完され、replay が成功する

#### Scenario: 新規イベントは org_id 必須
- **WHEN** 新規イベントを発行する
- **THEN** org_id が必須となる

### Requirement: org 別集計

システムは、BudgetLedger / NodeStatus / LiveTopology の projection を org_id 対応に拡張し、org 別集計を返さなければならない (SHALL)。

#### Scenario: org 別集計が返る
- **WHEN** org 別のコストを問い合わせる
- **THEN** org ごとの集計が返る
