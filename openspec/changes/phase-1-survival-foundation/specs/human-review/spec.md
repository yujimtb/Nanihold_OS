# human-review(N-10)

## ADDED Requirements

### Requirement: Discord 承認フロー

システムは、review 要求を内容・理由・期限付きで Discord に投稿し、許可ユーザーの ✅/❌ リアクションで resolve イベントを発行しなければならない (SHALL)。

#### Scenario: 承認・否認・放置の3パターンが動く
- **WHEN** review に対して承認・否認・放置(timeout)を行う
- **THEN** それぞれ正しく resolve され、replay で履歴が再現される

### Requirement: 期限超過の escalation

システムは、review の期限超過をデフォルトで拒否扱いとし、suspend へ escalation しなければならない (SHALL)。

#### Scenario: 放置は拒否扱いになる
- **WHEN** review の期限を超過する
- **THEN** 拒否扱いとなり suspend される

### Requirement: 適用対象ポリシー

システムは、閾値(例: 1,000円)超の見積もりを持つ invocation・すべての EXTERNAL_WRITE・商品定義外の受注を review 必須としなければならない (SHALL)。

#### Scenario: 高額見積もりは review される
- **WHEN** 閾値を超える見積もりの invocation を発行する
- **THEN** human review が要求される
