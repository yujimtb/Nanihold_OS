## ADDED Requirements

### Requirement: CRT-01 Luna 第一候補構成
システムは本番 coding route(`route_key = coding:personal-production`)の第一候補を `gpt-5.6-luna/xhigh` としなければならない(SHALL)。dispatcher は追加のエスカレーション判定なしに、当該 route の通常選択で Luna を選ばなければならない(SHALL)。

#### Scenario: 通常選択で Luna が選ばれる
- **GIVEN** `coding:personal-production` の PUBLISHED RouteSnapshot が Luna 第一候補構成を持つ
- **WHEN** dispatcher が当該 route の候補を選択する
- **THEN** `gpt-5.6-luna/xhigh` が第一候補として選ばれる

### Requirement: CRT-02 Sol 明示エスカレーション
システムは `gpt-5.6-sol/xhigh` を明示エスカレーション先として構成しなければならない(SHALL)。Sol は通常選択の第一候補になってはならず(SHALL NOT)、`docs/routing.md` "Coding escalation" が定めるエスカレーション条件が成立した場合にのみ選択されなければならない(SHALL)。

#### Scenario: 明示 override が成立すると Sol へ移る
- **GIVEN** Luna 実行が `docs/routing.md` のエスカレーション条件を満たす失敗を起こした
- **WHEN** override が評価される
- **THEN** WorkItem・未達 acceptance・gate 差分・artifact/decision ref だけを渡して `gpt-5.6-sol/xhigh` へ移る

#### Scenario: エスカレーションは自然発生のみ計測される
- **GIVEN** 本番 route が稼働している
- **WHEN** Escalation Trace を収集する
- **THEN** 人工的な発火は行われず、自然発生した Escalation Trace だけが計測される

### Requirement: CRT-03 エスカレーション判定の期待残 token 再計算
システムは失敗のたびに、Luna を続けた場合の期待残 token と、Sol へ移った場合の期待残 token を再計算しなければならない(SHALL)。固定 retry 回数を用いてはならない(SHALL NOT)。

#### Scenario: 失敗ごとに両側の期待残 token を再計算する
- **GIVEN** Luna 実行が失敗した
- **WHEN** エスカレーション判定を行う
- **THEN** Luna 継続の期待残 token と Sol 移行の期待残 token を再計算し、固定回数の retry に依らずに判定する

### Requirement: CRT-04 設定と docs の整合
システムは production 用 `vsm.toml` の routing/candidates と `docs/routing.md` の luna → sol override 記述が同一のエスカレーション意図を表すことを保証しなければならない(SHALL)。両者が矛盾する構成を許してはならない(SHALL NOT)。

#### Scenario: 設定と docs が一致する
- **GIVEN** production 用 `vsm.toml` の候補構成と `docs/routing.md` のエスカレーション記述
- **WHEN** 両者を突き合わせる
- **THEN** 第一候補・エスカレーション先・判定条件が一致する

### Requirement: CRT-05 承認制 RouteSnapshot 再発行
システムは coding route の候補変更を新 RouteSnapshot の再発行として反映しなければならない(SHALL)。後継を `register → S3_STAR_APPROVED → OWNER_APPROVED → PUBLISHED` へ進め、旧 `PUBLISHED` を `superseded_by_approved_snapshot` 理由の human Event で `RETIRED` にした後にのみ後継を `PUBLISHED` にしなければならない(SHALL)。旧版 retirement と後継 publish を一操作へまとめてはならない(SHALL NOT)。

#### Scenario: 承認を経て後継が publish される
- **GIVEN** Luna 第一候補構成の後継 snapshot 候補
- **WHEN** register から S3* 承認・owner 承認を経て publish する
- **THEN** 旧 PUBLISHED snapshot が先に RETIRED になってから後継が PUBLISHED になる

#### Scenario: 単一 routable snapshot の不変条件
- **GIVEN** 同一 `route_key` に別の PUBLISHED snapshot が残る
- **WHEN** 後継の publish を試みる
- **THEN** publish は fail fast し、先に明示 retirement を要求する
