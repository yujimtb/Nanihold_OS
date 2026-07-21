## ADDED Requirements

### Requirement: ACR-01 エージェント個名レジストリとローテーション自動割当
システムはエージェント(Nanihold の Node / Pilot 実行主体)へ個名を割り当てる台帳を持たなければならない(SHALL)。台帳は個名 ↔ `node_id` / `pilot_id` の写像を保持しなければならない(SHALL)。システムは WorkItem の受け渡し(dispatch)時に、当該 WorkItem を実行するエージェントへ個名を**自動割り当て**しなければならず(SHALL)、割り当てはタスクごとに新規付与し、名前プールをローテーションしなければならない(SHALL)。自動割当の目的は「どのエージェントが何をやっているか」の可視化であり、割り当てた個名は Ledger / receipt / チャネル通知に刻まれなければならない(SHALL)。

個名は名前プール `D:\userdata\docs\projects\_cutover_20260720_fable_activation\asset\Agent_name.csv`(列: カテゴリ, 規模, 意味座標, 日, 英, 羅, いいね)から採り、次の規則に従って選定しなければならない(SHALL):

- **規模 ↔ モデル階級**: 規模 3 = 旗艦(GPT `sol` / Claude `Fable`)、規模 2 = 中堅(GPT `terra` / Claude `Opus`)、規模 1 = 軽量(GPT `luna` / Claude `Sonnet` / Claude `Haiku`)。実行エージェントのモデル階級に対応する規模の行から採らなければならない(SHALL)。
- **言語 ↔ 系統**: Claude 系は日本語名(`日` 列)、GPT 系は英名(`英` 列)、その他プロバイダはラテン名(`羅` 列)を用いなければならない(SHALL)。
- **いいねフラグ**: `いいね` = 0 の行(重複マーク)は使用してはならない(SHALL NOT)。空欄および 1 の行は使用してよい(MAY)。
- **枯渇時**: プールが枯渇した場合は数字サフィックス(例: `Hayate2`)を付して継続しなければならない(SHALL)。
- **エフォートレベル**は命名と無関係であり(別管理)、命名選定に用いてはならない(SHALL NOT)。

なお Interface node(`node:owner-interface`)の予約名 `Nagi`(凪)はローテーション対象外であり(ACR-06 参照)、自動割当はこれを候補に含めてはならない(SHALL NOT)。

#### Scenario: dispatch 時に規模・系統に沿った個名が自動割り当てされる
- **GIVEN** ある WorkItem と、それを実行する Claude Opus のエージェント
- **WHEN** WorkItem を dispatch する
- **THEN** 規模 2 かつ `いいね` ≠ 0 の行の `日`(日本語名)列から個名が 1 つ自動選定され、当該エージェントへ割り当てられて台帳に記録される

#### Scenario: タスクごとに新規付与しプールをローテーションする
- **GIVEN** 同一エージェントが連続して 2 件の WorkItem を実行する
- **WHEN** それぞれの dispatch が行われる
- **THEN** タスクごとに新規の個名が付与され、名前プールがローテーションされる(前タスクの個名を固定的に再利用しない)

#### Scenario: いいね=0 の行は使用されない
- **GIVEN** `Agent_name.csv` に `いいね` = 0 の行(重複マーク)が存在する
- **WHEN** 自動割当が候補行を選ぶ
- **THEN** `いいね` = 0 の行は選ばれず、空欄または 1 の行のみが選定対象となる

#### Scenario: プール枯渇時は数字サフィックスで継続する
- **GIVEN** 規模・系統の条件に合致する未使用の個名が尽きた状態
- **WHEN** さらなる dispatch が発生する
- **THEN** 既存個名に数字サフィックス(例: `Hayate2`)を付して割り当てが継続される

#### Scenario: 個名が Ledger / receipt / 通知に刻まれる
- **GIVEN** dispatch で個名が割り当てられたエージェント
- **WHEN** そのエージェントが作業し、通知・receipt を残す
- **THEN** 割り当てられた個名が Ledger イベント・receipt・チャネル通知に帰属として刻まれ、「どのエージェントが何をやっているか」が復元できる

### Requirement: ACR-02 通知(inbound routing)
システムはチャネル着信のうちエージェントの個名を宛先とするものを、当該エージェントへの通知として配送しなければならない(SHALL)。

宛先の判定は次の記法に従わなければならない(SHALL):
- **主**: 本文文頭の `@名前`(例: `@Toki ...`)。
- **補**: リプライ / スレッドにおける宛先の継承(親メッセージの宛先を子へ引き継ぐ)。
- プレフィックス文字は**設定値**(例: Intercom 設定 `AGENT_ADDRESS_PREFIX`、既定 `"@"`)から解決しなければならず、コード内にハードコードしてはならない(SHALL NOT)。
- いずれの宛先規約にも合致しない着信は特定エージェントへ配送せず、既存の LETHE 観測取り込みに留めなければならない(SHALL)——誤配送より取りこぼしを優先する fail-safe とする。

配送は Nanihold Operational Ledger(実体 = personal-primary LETHE `:8080`、`space:personal-primary`)のイベントとして行うことを基盤とし(SHALL)、返信起草という作業単位が必要な場合は当該通知を WorkItem 起票へ昇格させる二段構えとしなければならない(SHALL)。宛先が特定できない着信を、任意のエージェントへ誤って配送してはならない(SHALL NOT)。

#### Scenario: 文頭 @名前 の着信が当該エージェントへ通知される
- **GIVEN** 本文文頭が `@Toki` で始まり、個名 `Toki` がレジストリに存在する着信
- **WHEN** inbound routing が宛先を解決する
- **THEN** `Toki` への通知として Nanihold Operational Ledger(personal-primary LETHE, `space:personal-primary`)へイベント配送される

#### Scenario: リプライ / スレッドで宛先が継承される
- **GIVEN** `@Toki` 宛の親メッセージに対するリプライ(宛先明記なし)
- **WHEN** inbound routing がスレッド文脈から宛先を解決する
- **THEN** 親の宛先 `Toki` が継承され、同エージェントへの通知として配送される

#### Scenario: プレフィックスは設定値から解決される
- **GIVEN** Intercom 設定 `AGENT_ADDRESS_PREFIX` が既定値 `"@"` である
- **WHEN** inbound routing が宛先記法を判定する
- **THEN** プレフィックス文字は設定値から解決され、ハードコードされた文字に依存しない(設定変更で規約が変わる)

#### Scenario: 宛先不明の着信は誤配送されない
- **GIVEN** どの個名にも、どの宛先規約にも合致しない着信
- **WHEN** inbound routing が宛先を解決しようとする
- **THEN** 特定エージェントへは配送されず、既存の LETHE 観測取り込みに留まる

#### Scenario: 作業が要る通知は WorkItem へ昇格する
- **GIVEN** Ledger へ配送されたエージェント宛通知のうち、返信起草という作業単位を要するもの
- **WHEN** 二段構えの昇格判定が行われる
- **THEN** 当該通知は WorkItem として起票され、実行系に乗る

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
システムは通知の配送と返信の帰属を Nanihold Operational Ledger / receipt で追跡可能にしなければならない(SHALL)。通知配送は「どの着信を・どの個名(エージェント)へ・どの形態で配送したか」を、返信は「どの個名が起草し・どの承認を経て・どの着信への応答か」を、後から辿れる記録として残さなければならない(SHALL)。自動割当された個名は当該作業の receipt に帰属として刻まれ、「どのエージェントが何をやっているか」を後から復元できなければならない(SHALL)。

#### Scenario: 通知配送が監査できる
- **GIVEN** ある着信がエージェントへ通知された
- **WHEN** 監査記録を辿る
- **THEN** 着信 → 宛先個名 → 配送形態(Ledger イベント id / WorkItem id)の対応が Ledger / receipt から復元できる

#### Scenario: 返信帰属が監査できる
- **GIVEN** あるエージェントが起草し承認・配信された返信
- **WHEN** 監査記録を辿る
- **THEN** 起草した個名・承認(`reply-approval@1`)・応答先の着信が紐付いて復元できる

#### Scenario: dispatch で割り当てた個名が作業に紐づく
- **GIVEN** dispatch で個名を自動割り当てされたエージェントが WorkItem を実行した
- **WHEN** 監査記録を辿る
- **THEN** 個名 ↔ WorkItem ↔ receipt が紐づき、どのエージェントがどの作業を担ったかが復元できる

### Requirement: ACR-05 スコープ境界(Non-Goals の固定)
システムは本 change の境界を維持しなければならない(SHALL)。返信文の自動生成、承認を経ない自動送信は提供してはならない(SHALL NOT)。また `lethe-channel-bridge` の import / card-queue / send 契約を本 change で変更してはならない(SHALL NOT)——本機構はその consumer / producer に徹する。個名の自動割当は本 change の設計対象であり(ACR-01)、Non-Goal ではない。

#### Scenario: 返信の自動化禁止事項が守られる
- **GIVEN** 通知・返信の運用
- **WHEN** 機構が動作する
- **THEN** 返信自動生成・承認レス自動送信はいずれも行われない

#### Scenario: 既存ブリッジ契約を変更しない
- **GIVEN** 既存の `lethe-channel-bridge` 契約(import / card-queue / send)
- **WHEN** 本機構が着信通知・返信投入を行う
- **THEN** 既存契約は変更されず、本機構は既存の producer / consumer 面のみを用いる

### Requirement: ACR-06 Nagi S5 常設席(ローテーション対象外の予約名)
システムは Interface node(`node:owner-interface`)への割当名 `Nagi`(凪)を、手動割当済みの予約名として扱わなければならない(SHALL)。`Nagi` はローテーションの対象外であり、ACR-01 の自動割当はこれを付与・再利用してはならない(SHALL NOT)。`Nagi` は S5(最上位)としてタスク完了条件(終了条件)を持たない常設の席として扱わなければならず(SHALL)、WorkItem の受け入れ条件体系の外に置かなければならない(SHALL)。名前は席に属し、搭乗するパイロット(実行主体)の交代があっても不変でなければならない(SHALL)。

#### Scenario: Nagi はローテーションで払い出されない
- **GIVEN** ACR-01 のローテーション自動割当
- **WHEN** dispatch で個名を選定する
- **THEN** `Nagi`(凪)は候補から除外され、他エージェントへ払い出されない

#### Scenario: Nagi は終了条件を持たない常設席である
- **GIVEN** `node:owner-interface` に割り当てられた `Nagi`
- **WHEN** WorkItem の受け入れ・完了条件を評価する
- **THEN** `Nagi` の席は完了条件(終了条件)を持たず、WorkItem の受け入れ条件体系の外にあるものとして扱われる

#### Scenario: 名前はパイロット交代でも不変である
- **GIVEN** `Nagi` の席に搭乗するパイロットが交代する
- **WHEN** 交代が行われる
- **THEN** 席の名前 `Nagi` は変わらず、名前は席に帰属したまま維持される
