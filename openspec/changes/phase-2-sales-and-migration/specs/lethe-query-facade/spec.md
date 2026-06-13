# lethe-query-facade(N-16)

## ADDED Requirements

### Requirement: lethe_query Tool facade

システムは、lethe_query(EXTERNAL_READ)を提供し、network_scope を LETHE の URL のみに制限しなければならない (SHALL)。

#### Scenario: scope 外 URL への接続が失敗する
- **WHEN** scope 外の URL に接続する
- **THEN** テストで失敗する

#### Scenario: LETHE へ疎通する
- **WHEN** network_scope が LETHE URL のみに制限されたサービス Node から lethe_query を呼ぶ
- **THEN** LETHE へ疎通する

### Requirement: 読取結果の保存方針

システムは、lethe_query の結果のうち「クエリした事実(ToolInvocation 記録)」と「派生した結論」のみを Event_Log に保存し、レスポンス本文の生データを保存してはならない (MUST NOT)。

#### Scenario: 生データを保存しない
- **WHEN** lethe_query の結果を記録する
- **THEN** 生データ本文は保存されず、provenance(observation 参照)のみが残る
