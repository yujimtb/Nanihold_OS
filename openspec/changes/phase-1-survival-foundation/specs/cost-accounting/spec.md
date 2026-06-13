# cost-accounting(N-1)

## ADDED Requirements

### Requirement: invocation 単位のコスト記録

システムは、LLM 呼び出しごとに CostRecorded イベントを発行し、run_id・node_id(N-12 後は org_id・customer_id)を付与して BudgetLedger に集計しなければならない (SHALL)。

#### Scenario: Run コストが円で即答できる
- **WHEN** vsm CLI または Web UI で Run のコストを問い合わせる
- **THEN** 円で即座に返る

#### Scenario: 合計が手計算と一致する
- **WHEN** ダミー Run を10本実行する
- **THEN** 合計コストが手計算と一致する

### Requirement: 為替換算の一貫性

システムは、USD 建てコストを月初固定レートで円換算し、使用したレートをイベントに記録しなければならない (SHALL)。

#### Scenario: レートがイベントに残る
- **WHEN** CostRecorded を発行する
- **THEN** 換算に使ったレートがイベントに記録される
