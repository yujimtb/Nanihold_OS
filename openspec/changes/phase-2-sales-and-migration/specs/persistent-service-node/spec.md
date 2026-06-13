# persistent-service-node(N-15)

## ADDED Requirements

### Requirement: 終了しないサービス Node のライフサイクル

システムは、サービス Node を「終了しないこと」を正常とし、起動・停止のライフサイクルイベント、ハートビート(watchdog 接続)、クラッシュ時の自動再起動(起動回数の記録)を持たせなければならない (SHALL)。

#### Scenario: 再起動をまたいで継続する
- **WHEN** サービス Node がプロセス再起動する
- **THEN** 同一 node_id で継続し、停止期間が Event_Log から判別できる

### Requirement: リクエスト単位の会計

システムは、サービス Node のリクエストを 1質問 = 1課金単位として CostRecorded に customer_id 付きで記録しなければならない (SHALL)。

#### Scenario: 顧客別に課金が乗る
- **WHEN** サービス Node が質問を処理する
- **THEN** customer_id 付きで CostRecorded が記録される
