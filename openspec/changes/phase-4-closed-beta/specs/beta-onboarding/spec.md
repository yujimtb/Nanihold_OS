# beta-onboarding

## ADDED Requirements

### Requirement: 提供形態2択のオンボーディング

システムは、当方ホスティング(org 別 SQLite 分離)と先方 self-host(docker compose)の2択でオンボーディングし、同意取得確認 → トークン発行 → チャンネル選定 → 初回 sync → スキーマ設定 → 初回推論 → 結果説明会、のチェックリストに従わなければならない (SHALL)。

#### Scenario: チェックリスト順に立ち上がる
- **WHEN** 新規組織をオンボーディングする
- **THEN** チェックリスト順に立ち上がり、初回推論と結果説明会まで到達する

### Requirement: 受け入れ上限

システムは、β受け入れを最大3組織に制限しなければならない (SHALL)。

#### Scenario: 上限を超えない
- **WHEN** 4組織目の希望が来る
- **THEN** 受け入れ上限により保留される
