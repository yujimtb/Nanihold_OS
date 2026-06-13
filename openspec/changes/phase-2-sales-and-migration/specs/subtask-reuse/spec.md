# subtask-reuse(N-17)

## ADDED Requirements

### Requirement: 過去サブタスクの索引と提示

システムは、過去 Run のタスク記述・成果参照を索引化(v1 は全文検索)し、新規受託の分解時に S3/S4 が再利用候補を提示しなければならない (SHALL)。

#### Scenario: 過去成果が参照される
- **WHEN** 2件目の受託を分解する
- **THEN** 1件目またはチャットボット案件の成果(コード断片・手順)が参照されたことが Event_Log 上で確認できる
