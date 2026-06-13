# lethe-retention(L-8)

## ADDED Requirements

### Requirement: 人単位の連鎖削除

システムは、人単位の削除で observation・identity・projection・blob を連鎖削除し、AuditLog に削除記録を残した上で実データを物理削除しなければならない (SHALL)。

#### Scenario: 削除後はデータが消え記録だけ残る
- **WHEN** ダミー人物を削除する
- **THEN** 全 API・全ストレージ(blob 含む)から該当データが消え、削除記録だけが残る

### Requirement: 保持期間ポリシーの自動失効

システムは、org 設定の保持期間ポリシーにより観測を自動失効させなければならない (SHALL)。

#### Scenario: 期限超過で失効する
- **WHEN** 観測が保持期間を超える
- **THEN** 該当観測が自動失効する
