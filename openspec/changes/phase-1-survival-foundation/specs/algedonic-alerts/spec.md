# algedonic-alerts(N-5)

## ADDED Requirements

### Requirement: 3段階 severity の警報

システムは、WARN・ALERT・PAGE の3段階 severity で警報を出し分けなければならない (SHALL)。

#### Scenario: WARN は日次レポートに集約する
- **WHEN** 日次予算 70% 消費・単発 Run 失敗が起きる
- **THEN** 即時通知せず日次レポートに集約する

#### Scenario: ALERT は Discord に即時通知する
- **WHEN** 日次予算 100%・同一 Node 連続3失敗・外部 API 認証エラーが起きる
- **THEN** Discord に即時通知する

#### Scenario: PAGE は mention で通知する
- **WHEN** kill switch 発動・health 喪失・月次予算逸脱ペース・セキュリティ事象が起きる
- **THEN** Discord @mention +(可能なら)メールで通知する

### Requirement: 階層バイパス経路

システムは、algedonic 通知を S1→S2→S3 の通常階層を経由させず、EscalationFacade から直接 Discord adapter へ送らなければならない (SHALL)。

#### Scenario: 通常階層を経由しない
- **WHEN** 警報を発火する
- **THEN** S1→S2→S3 を経由せず直接通知される

### Requirement: 通知ループの防止

システムは、通知失敗が新たな通知を呼ぶ無限ループを起こしてはならない (MUST NOT)。

#### Scenario: 通知失敗でループしない
- **WHEN** 通知が失敗する
- **THEN** 通知失敗を起点とした無限ループが発生しない
