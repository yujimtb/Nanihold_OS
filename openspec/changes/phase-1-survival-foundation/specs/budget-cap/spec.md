# budget-cap(N-2)

## ADDED Requirements

### Requirement: 二段のハードキャップ判定

システムは、日次・月次の上限(org 単位 + グローバル)を ToolInvocation 発行前(事前見積)と CostRecorded 時(事後確定)の二段で判定しなければならない (SHALL)。

#### Scenario: 事前見積で超過を止める
- **WHEN** 発行前の見積がキャップを超える
- **THEN** 新規 invocation を拒否する

### Requirement: 枯渇時の確定挙動

システムは、予算枯渇時に新規 invocation を拒否し、該当 Node を suspend し、EscalationFacade 経由で algedonic 通知を出さなければならない (SHALL)。

#### Scenario: 暴走 Run がキャップで停止し通知される
- **WHEN** 無限ループする安価 LLM 呼び出し Run を放置する
- **THEN** キャップで停止し、Discord に通知が届く

### Requirement: 自動引き上げの禁止

システムは、予算キャップを自動で引き上げる経路を提供してはならない (SHALL NOT)。引き上げは人間のみが行える。

#### Scenario: 自動引き上げが存在しない
- **WHEN** キャップに到達する
- **THEN** 自動引き上げは行われず、人間の操作なしには上限が変わらない
