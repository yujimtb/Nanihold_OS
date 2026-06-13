# lifecycle-facades(N-9)

## ADDED Requirements

### Requirement: terminate/suspend/resume の CONTROL facade

システムは、terminate・suspend・resume を CONTROL effect として提供し、ParentAuthority 検証で親系列以外(横・下)からの制御を拒否しなければならない (SHALL)。

#### Scenario: 権限外の制御は拒否される
- **WHEN** 権限のない Node が制御を呼び出す
- **THEN** 拒否され、その拒否がイベントに記録される

### Requirement: suspend の安全停止

システムは、suspend を「実行中 invocation の完了を待ってから停止」をデフォルトとし、即時中断を terminate に限定しなければならない (SHALL)。

#### Scenario: suspend → resume で状態を失わない
- **WHEN** Run を suspend して resume する
- **THEN** Run が状態を失わない
