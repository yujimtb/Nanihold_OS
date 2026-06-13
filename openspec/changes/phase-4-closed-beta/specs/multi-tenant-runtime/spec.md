# multi-tenant-runtime(N-24)

## ADDED Requirements

### Requirement: マルチテナント実運用

システムは、org 単位の Event_Log・ParentAuthority・予算分離を実トラフィックで成立させ、org 別の日次レポートを出さなければならない (SHALL)。

#### Scenario: 相互干渉ゼロで稼働する
- **WHEN** 3 org を同時稼働する
- **THEN** 相互干渉ゼロで、org 別の日次レポートが出る
