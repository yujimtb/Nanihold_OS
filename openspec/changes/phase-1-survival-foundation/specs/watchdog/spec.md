# watchdog(N-8)

## ADDED Requirements

### Requirement: ハートビート監視と自動 suspend

システムは、Node/Run のハートビート(最終活動時刻)を NodeStatus projection に記録し、閾値(デフォルト: ツール応答待ち以外で30分無活動)超過で ALERT と自動 suspend(設定により terminate)を行わなければならない (SHALL)。

#### Scenario: stuck を検出して止める
- **WHEN** 人工的に sleep する Run を仕込む
- **THEN** 検出 → suspend → 通知の一連が動く
