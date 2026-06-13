# kill-switch(N-3)

## ADDED Requirements

### Requirement: 3経路からの全停止

システムは、全 Node を suspend する単一操作を CLI・Web UI ボタン・Discord コマンドの3経路から提供し、スケジューラも停止対象に含めなければならない (SHALL)。

#### Scenario: 10秒以内に全停止する
- **WHEN** Run 3本 + 常駐 Node が稼働中の状態で kill switch を発動する
- **THEN** 10秒以内に全停止する

#### Scenario: resume で再開する
- **WHEN** 発動後に resume する
- **THEN** 停止した Run / Node が再開する

### Requirement: 発動の記録

システムは、kill switch の発動と解除を Event_Log に記録しなければならない (SHALL)。

#### Scenario: 発動・解除がイベントに残る
- **WHEN** kill switch を発動・解除する
- **THEN** KillSwitchActivated / Released が Event_Log に記録される
