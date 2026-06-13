# automation-levels(N-23)

## ADDED Requirements

### Requirement: Lv0〜Lv3 の自動化レベル

システムは、自動化レベル Lv0〜Lv3 を ObservedUnit(Node)単位の設定として実装し、初期値を Lv0、引き上げを human review 必須かつ Event_Log 記録としなければならない (SHALL)。組織全体を一括で Lv3 にする操作を存在させてはならない (MUST NOT)。

#### Scenario: Lv0 では書き込みが構造的に不可能
- **WHEN** Lv0 の unit に対して EXTERNAL_WRITE を試行する
- **THEN** 権限検証で拒否され、構造的に不可能である

#### Scenario: 引き上げは承認と記録を伴う
- **WHEN** ある unit の自動化レベルを引き上げる
- **THEN** human review を経て AutomationLevelChanged が Event_Log に記録される
