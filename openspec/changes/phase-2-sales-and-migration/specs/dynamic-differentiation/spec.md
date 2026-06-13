# dynamic-differentiation(N-18)

## ADDED Requirements

### Requirement: 代表シナリオの回帰固定

システムは、代表シナリオ3本(A 単純一括 / B 中規模受託 / C 複合)を現行固定フローで実行し、replay 可能な回帰テストとして固定しなければならない (SHALL)。

#### Scenario: 3シナリオが回帰化される
- **WHEN** 代表シナリオ3本を固定フローで実行する
- **THEN** replay 可能な回帰テストとして固定される

### Requirement: 動的分化判断

システムは、タスク複雑度見積もり × budget 残に基づく分化判断を S5/S3 に実装し、フラグで動的モードへ切り替えられなければならない (SHALL)。固定フローはフォールバックとして残す。

#### Scenario: 小タスクは過剰分化しない
- **WHEN** シナリオ A を動的モードで実行する
- **THEN** 固定フロー以下のコストで、過剰分化しない

#### Scenario: 複合は並行分化する
- **WHEN** シナリオ C を実行する
- **THEN** 子 u-VSM に分化して並行実行される(すべて replay 可能)
