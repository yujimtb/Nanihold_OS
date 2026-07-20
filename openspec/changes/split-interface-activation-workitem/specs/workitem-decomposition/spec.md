## ADDED Requirements

### Requirement: WD-01 巨大 WorkItem の廃棄
システムは WorkItem `work:interface-effective-activity-start` を廃棄しなければならない(SHALL)。廃棄後、当該 WorkItem をそのまま再投入してはならない(SHALL NOT)。

#### Scenario: 巨大 WorkItem が廃棄される
- **GIVEN** WorkItem `work:interface-effective-activity-start` が存在する
- **WHEN** 本 change を適用する
- **THEN** 当該 WorkItem は廃棄され、実行キューへ再投入されない

### Requirement: WD-02 既達条件の除外
システムは既達条件(ReorientationAssessment 提示済み・owner 承認済み ACTIVE)を分割後 WorkItem 群から除外しなければならない(SHALL)。既達条件を再実行の対象にしてはならない(SHALL NOT)。既達の認定はオーナー承認事項とする。

#### Scenario: 既達条件は再実行されない
- **GIVEN** ReorientationAssessment が提示済みで owner 承認済み ACTIVE である
- **WHEN** 残作業を小粒 WorkItem へ棚卸しする
- **THEN** これら既達条件は新規 WorkItem に含まれない

### Requirement: WD-03 小粒 WorkItem の粒度要件
システムは新規に起票する各 WorkItem を `1 WorkItem = 12k トークン / 300 秒で完了可能な粒度` に縛らなければならない(SHALL)。起票時に当該粒度を超える見込みの WorkItem は、起票前に更に分割しなければならない(SHALL)。

#### Scenario: 粒度超過は起票時に再分割される
- **GIVEN** 棚卸しで見つかった残作業のうち 12k/300 秒で完了不能な見込みのもの
- **WHEN** WorkItem として起票する
- **THEN** 起票前に 12k/300 秒で完了可能な複数 WorkItem へ分割される

#### Scenario: acceptance criteria が粒度を明示する
- **GIVEN** 新規 WorkItem のドラフト
- **WHEN** その acceptance criteria を確認する
- **THEN** 「12k トークン / 300 秒で完了可能」であることが acceptance criteria に含まれる

### Requirement: WD-04 budget 制限値の維持
システムは `token_budget = 12k` / `300` 秒の制限値を維持しなければならない(SHALL)。本 change の中で当該制限値を緩めてはならない(SHALL NOT)。

#### Scenario: 制限値が変更されない
- **GIVEN** 現行の budget 制限値 12k トークン / 300 秒
- **WHEN** 本 change を適用する
- **THEN** 制限値は 12k / 300 秒のまま維持される

### Requirement: WD-05 分割リストのオーナー承認
システムは最終の分割 WorkItem リストをオーナー承認事項として扱わなければならない(SHALL)。分割案の骨子(チャネルブリッジ検収・未完タスク棚卸しからの次期 WorkItem 起票・実装系タスク群)は提案として提示し、オーナー承認を経ずに確定リストとして起票してはならない(SHALL NOT)。

#### Scenario: 承認前は確定しない
- **GIVEN** 分割案の骨子が提案として提示されている
- **WHEN** オーナー承認がまだ得られていない
- **THEN** 分割リストは確定として起票されず、承認待ち状態に留まる
