## ADDED Requirements

### Requirement: ACR-01 エージェント個名レジストリ
システムはエージェント(Nanihold の Node / Pilot 実行主体)へ個名を割り当てる台帳を持たなければならない(SHALL)。台帳は個名 ↔ `node_id` / `pilot_id` の写像を保持し、個名は名前プール(`Agent_name.csv`:カテゴリ[居/糸/器/水/木/天候/地]・規模 1-3・意味座標・日本語名ローマ字・英名・ラテン名・いいねフラグ)から採らなければならない(SHALL)。個名の割り当てはオーナー承認事項として扱わなければならず(SHALL)、システムは名前プールから個名を自動割り当てしてはならない(SHALL NOT)。

#### Scenario: 個名が node/pilot に写像される
- **GIVEN** 個名レジストリと名前プール `Agent_name.csv`
- **WHEN** あるエージェント(`node_id` または `pilot_id`)へ個名を割り当てる
- **THEN** 台帳に個名 ↔ 当該 id の写像が 1 対 1 で記録される

#### Scenario: 割り当てはオーナー承認を要する
- **GIVEN** 未割り当てのエージェントと名前プール
- **WHEN** システムが個名を付与しようとする
- **THEN** オーナー承認なしに自動割り当てされず、承認待ちに留まる

#### Scenario: 個名は名前プール由来である
- **GIVEN** 割り当て候補の個名
- **WHEN** レジストリへ登録する
- **THEN** その個名は `Agent_name.csv` の行に由来し、プール外の任意名は登録されない

### Requirement: ACR-02 通知(inbound routing)
システムはチャネル着信のうちエージェントの個名を宛先とするものを、当該エージェントへの通知として配送しなければならない(SHALL)。宛先の判定規約(先頭「名前:」・メンション等)は設計提案として提示し、確定はオーナー承認事項として扱わなければならない(SHALL)。配送先の形態(Nanihold Ledger イベント / 実行中 Execution への注入 / 新規 WorkItem 起票)は選択肢比較の上で推奨を提示し(SHALL)、確定はオーナー承認事項とする(SHALL)。宛先が特定できない着信を、任意のエージェントへ誤って配送してはならない(SHALL NOT)。

#### Scenario: 名指しの着信が当該エージェントへ通知される
- **GIVEN** エージェント個名を宛先とする(宛先規約に合致する)着信
- **WHEN** inbound routing が宛先を解決する
- **THEN** 当該エージェントへの通知として、承認された配送形態で配送される

#### Scenario: 宛先不明の着信は誤配送されない
- **GIVEN** どの個名にも合致しない着信
- **WHEN** inbound routing が宛先を解決しようとする
- **THEN** 特定エージェントへは配送されず、既存の LETHE 観測取り込みに留まる

#### Scenario: 規約と配送形態は承認事項として提示される
- **GIVEN** 宛先規約案と配送形態 3 案(Ledger / Execution 注入 / WorkItem 起票)
- **WHEN** 本 change をレビューする
- **THEN** 推奨付きで提示され、確定はオーナー承認を要する

### Requirement: ACR-03 返信(outbound authoring)
システムはエージェント自身が返信文を書き、書き手のエージェント個名を帰属(attribution)として付与した `reply-draft@1` を card-queue へ投入する経路を提供しなければならない(SHALL)。投入された返信ドラフトはオーナー承認(`reply-approval@1`)を経てから、既存の `lethe-channel-bridge` の配信経路で送信されなければならない(SHALL)。システムは返信文の自動生成ジェネレータを設けてはならず(SHALL NOT)、承認を経ない返信を配信してはならない(SHALL NOT)。

#### Scenario: エージェントが帰属付きで返信を起草する
- **GIVEN** 通知を受けたエージェントが返信文を書いた
- **WHEN** その返信を card-queue へ投入する
- **THEN** `reply-draft@1` に書き手のエージェント個名が帰属として付与される

#### Scenario: 承認を経て既存経路で配信される
- **GIVEN** card-queue 上の帰属付き `reply-draft@1`
- **WHEN** オーナーが `reply-approval@1` で承認する
- **THEN** 既存の `lethe-channel-bridge` 配信経路で送信され、新たな送信経路は作られない

#### Scenario: 自動生成・未承認配信をしない
- **GIVEN** 返信対象の通知
- **WHEN** システムが返信を扱う
- **THEN** 返信文は自動生成されず、オーナー承認のない返信は配信されない

### Requirement: ACR-04 監査
システムは通知の配送と返信の帰属を Nanihold Ledger / receipt で追跡可能にしなければならない(SHALL)。通知配送は「どの着信を・どの個名(エージェント)へ・どの形態で配送したか」を、返信は「どの個名が起草し・どの承認を経て・どの着信への応答か」を、後から辿れる記録として残さなければならない(SHALL)。

#### Scenario: 通知配送が監査できる
- **GIVEN** ある着信がエージェントへ通知された
- **WHEN** 監査記録を辿る
- **THEN** 着信 → 宛先個名 → 配送形態の対応が Ledger / receipt から復元できる

#### Scenario: 返信帰属が監査できる
- **GIVEN** あるエージェントが起草し承認・配信された返信
- **WHEN** 監査記録を辿る
- **THEN** 起草した個名・承認(`reply-approval@1`)・応答先の着信が紐付いて復元できる

### Requirement: ACR-05 スコープ境界(Non-Goals の固定)
システムは本 change の境界を維持しなければならない(SHALL)。返信文の自動生成、承認を経ない自動送信、名前プールからの自動割り当ては提供してはならない(SHALL NOT)。また `lethe-channel-bridge` の import / card-queue / send 契約を本 change で変更してはならない(SHALL NOT)——本機構はその consumer / producer に徹する。

#### Scenario: 自動化の禁止事項が守られる
- **GIVEN** 通知・返信の運用
- **WHEN** 機構が動作する
- **THEN** 返信自動生成・承認レス自動送信・個名の自動割り当てはいずれも行われない

#### Scenario: 既存ブリッジ契約を変更しない
- **GIVEN** 既存の `lethe-channel-bridge` 契約(import / card-queue / send)
- **WHEN** 本機構が着信通知・返信投入を行う
- **THEN** 既存契約は変更されず、本機構は既存の producer / consumer 面のみを用いる
