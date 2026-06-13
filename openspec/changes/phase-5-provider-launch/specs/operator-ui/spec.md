# operator-ui(N-26)

## ADDED Requirements

### Requirement: 非エンジニア向け操作面

システムは、タスク投入・承認(review queue)・構造確認(O5)・レポート閲覧を Web UI で完結させ、CLI や events.jsonl を見せずに日常運用が回るようにしなければならない (SHALL)。

#### Scenario: CLI なしで日常運用が回る
- **WHEN** 非エンジニアの運営者が日常運用を行う
- **THEN** タスク投入 / 承認 / 構造確認 / レポート閲覧が Web UI で完結する

### Requirement: ユーザビリティ判定

システムは、βの運営者に操作面を試用してもらい、ユーザビリティを判定しなければならない (SHALL)。

#### Scenario: β運営者が判定する
- **WHEN** βの運営者に操作面を触ってもらう
- **THEN** ユーザビリティの判定結果が得られる
