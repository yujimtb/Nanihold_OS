# lethe-ingestion-gate(L-7)

## ADDED Requirements

### Requirement: 取り込み時の consent 評価

システムは、取り込み時点で consent policy を評価し、オプトイン外チャンネル・ユーザーの観測を保存前に破棄しなければならない (SHALL)。保存してから隠すのではなく、入れない。

#### Scenario: オプトイン外は保存されない
- **WHEN** オプトイン外チャンネルのメッセージを取り込もうとする
- **THEN** Lake に存在しないことを直接検証できる
