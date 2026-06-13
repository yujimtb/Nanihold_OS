# scheduler(N-6)

## ADDED Requirements

### Requirement: schedule 定義と発火

システムは、cron 式または interval の schedule 定義に基づき、発火ごとに ScheduleFired イベントを発行して Run を生成しなければならない (SHALL)。

#### Scenario: 発火で Run が生成される
- **WHEN** schedule が発火する
- **THEN** ScheduleFired イベントが出て Run が生成される

### Requirement: 発火の冪等性

システムは、同一 schedule・同一予定時刻の二重発火を schedule_id + 予定時刻のキーで排除しなければならない (SHALL)。

#### Scenario: 重複起動でも二重発火しない
- **WHEN** 再起動・時刻またぎ・重複起動の条件下で発火する
- **THEN** 発火回数が仕様どおりで、replay で発火履歴が再現される

### Requirement: catch-up ポリシー

システムは、停止期間中の未発火について「最新1回のみ実行」をデフォルトの catch-up ポリシーとしなければならない (SHALL)。

#### Scenario: 停止後は最新1回のみ
- **WHEN** 停止期間をまたいで再開する
- **THEN** 未発火分は最新1回のみ実行される
