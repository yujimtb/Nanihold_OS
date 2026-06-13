# lethe-tenant(L-4)

## ADDED Requirements

### Requirement: org/tenant のファイル分離

システムは、observation・identity・projection・API のすべてに org_id を導入し、SQLite をテナントごとにファイル分離(`data/{org_id}/lethe.sqlite3`)しなければならない (SHALL)。

#### Scenario: 2 org が同居して稼働する
- **WHEN** 自社 + ダミーの2 org を同居稼働する
- **THEN** それぞれが独立したファイルで動く

### Requirement: クロステナント分離

システムは、org をまたぐクエリを存在させず、トークンを org に紐付けて他 org への到達を遮断しなければならない (SHALL)。

#### Scenario: 他 org のデータに到達できない
- **WHEN** トークン A(org A)で org B のデータを照会する
- **THEN** 一切到達できないことがテストで保証される
