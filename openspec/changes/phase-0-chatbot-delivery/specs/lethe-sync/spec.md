# lethe-sync(L-3)

## ADDED Requirements

### Requirement: 定期 sync の自動実行

システムは、`POST /admin/sync` を `admin:sync` 専用トークンで定期実行しなければならない (SHALL)。

#### Scenario: cron 発火で sync が実行される
- **WHEN** スケジュール(cron)が発火する
- **THEN** `admin:sync` トークンで `/admin/sync` が呼ばれ、最新データが Lake に取り込まれる

#### Scenario: 専用トークン以外では実行できない
- **WHEN** `admin:sync` を持たない経路から定期 sync を起動しようとする
- **THEN** 認可エラーとなり sync は実行されない(lethe-api-auth の scope 検証に従う)

### Requirement: 同期失敗の通知接続

システムは、sync 失敗を握り潰さず、ログへ明示エラーを出して通知に接続しなければならない (SHALL)。

#### Scenario: sync 失敗が検知・通知される
- **WHEN** 定期 sync が失敗する(取得元エラー・ネットワーク断等)
- **THEN** ログに明示的なエラーが記録され、その失敗が通知経路に接続される
