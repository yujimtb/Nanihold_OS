# lethe-discord-adapter(L-6)

## ADDED Requirements

### Requirement: Discord adapter

システムは、Slack adapter と同型の Discord adapter(channel 指定・thread 追跡・observation 化)を提供しなければならない (SHALL)。

#### Scenario: 自社 Discord が Lake に入る
- **WHEN** 自社 Discord の指定チャンネルを取り込む
- **THEN** Lake に入り、identity 解決が Slack 側の人物と統合される
