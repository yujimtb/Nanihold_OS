# structure-inference(N-21)

## ADDED Requirements

### Requirement: O3 構造推論バッチ

システムは、特徴抽出(活動統計、LLM 不使用)→ 安価 LLM による役割分類 → 指数移動平均での集約 → HumanCorrection の制約反映、という週次バッチで ObservedRole/ObservedEdge を更新しなければならない (SHALL)。

#### Scenario: 妥当な構造が出る
- **WHEN** 自社(2人 + エージェント)と SHIMOKITA データでバッチを回す
- **THEN** 人間が見て妥当な構造が出る

### Requirement: HumanCorrection の優先

システムは、人間が確定した役割を推論で上書きしてはならない (MUST NOT)。

#### Scenario: 確定は上書きされない
- **WHEN** HumanCorrection のある対象を再推論する
- **THEN** 確定値が上書きされない

### Requirement: 推論コストの上限

システムは、推論バッチに org あたりの上限額(budget-cap 配下)を設けなければならない (SHALL)。

#### Scenario: 上限内に収まる
- **WHEN** 推論バッチを実行する
- **THEN** org あたりの上限額を超えない
