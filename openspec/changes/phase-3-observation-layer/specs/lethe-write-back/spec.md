# lethe-write-back(L-9)

## ADDED Requirements

### Requirement: Command/EffectPlan → ToolInvocation 変換

システムは、LETHE の Command/EffectPlan を Nanihold の ToolInvocation(EXTERNAL_WRITE、idempotency_key = Command の決定的ハッシュ)へ変換し、リトライ・冪等・承認を Nanihold 側の責務としなければならない (SHALL)。

#### Scenario: 承認を経てのみ反映される
- **WHEN** Notion person page の1フィールドを更新する
- **THEN** Nanihold の human review 承認を経てのみ反映される(E2E)

### Requirement: LETHE 側の write 責務限定

システムは、LETHE 側の書き込みを `write:` scope のエンドポイントと AuditLog 記録のみに限定しなければならない (SHALL)。

#### Scenario: write は scope と AuditLog を伴う
- **WHEN** write 用エンドポイントが呼ばれる
- **THEN** `write:` scope のトークンが必須で、AuditLog に記録される
