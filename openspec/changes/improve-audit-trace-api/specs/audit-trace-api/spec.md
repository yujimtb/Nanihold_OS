## ADDED Requirements

<!--
要件群は本番パイプラインの 2 つの WorkItem に対応して 2 群に分ける。
- 群 1(work:audit-trace-concurrency 並列安全化): ATA-01, ATA-02
- 群 2(work:audit-trace-performance 性能改善 + 正しさ/非破壊): ATA-03, ATA-04, ATA-05
本 change は仕様・設計のみ。実装しない。
-->

### Requirement: ATA-01 並列トレース要求の相互非ブロッキング

システムは複数の監査トレース要求(`GET /api/audit-traces/notifications/{id}` および `GET /api/audit-traces/executions/{id}`)が同時に到来しても、それらが相互にブロックしないようにしなければならない(SHALL)。同時に受け付けた各要求はいずれも成功し、それぞれ正しいトレース結果を返さなければならない(SHALL)。単一ワーカー(`uvicorn workers=1`)構成であっても、一方のトレース処理が他方の完了を待たせてはならない(SHALL NOT)。

システムは高コストな監査トレース読み取りに対して並行度の上限(設定値から解決し、コードにハードコードしない)を設けてよい(MAY)。上限を超える要求は、無言でぶら下げるのではなく、決定的なビジー応答(例: HTTP 503 と再試行指示)を速やかに返さなければならない(SHALL)。

#### Scenario: 2 件並行の監査トレースが両方成功する(現状障害の再現反証)
- **GIVEN** 本番相当規模(約 5 万イベント)の Operational Ledger と、有効な notification_id と execution_id
- **WHEN** `GET /api/audit-traces/notifications/{id}` と `GET /api/audit-traces/executions/{id}` を 2 件並行で呼び出す
- **THEN** 両方の要求が(クライアントタイムアウト前に)成功応答を返し、いずれも正しいトレース結果を含む(直列でしか成功しない現状に対する反証)

#### Scenario: 一方のトレースが他方をブロックしない
- **GIVEN** 監査トレース要求 A が処理中である
- **WHEN** 別の監査トレース要求 B が到来する
- **THEN** B は A の完了を待たされず、独立して進行・完了する

#### Scenario: 並行度上限を超えた要求は無言でぶら下がらない
- **GIVEN** 監査トレースの並行度上限(設定値)に達している状態
- **WHEN** さらなる監査トレース要求が到来する
- **THEN** その要求は無言でタイムアウトまでぶら下がるのではなく、決定的なビジー応答(再試行指示付き)を速やかに受け取る

### Requirement: ATA-02 クライアント切断後のサーバ側処理の有界化

システムは、監査トレース要求のクライアントが切断・タイムアウトした後に、当該要求のサーバ側処理(Operational Ledger / LETHE への読み取り往復を含む)を無期限に継続してはならない(SHALL NOT)。システムはクライアント切断を検知し、以降の台帳読み取り(新規の `page()` / `stream()` 発行)を速やかに打ち切らなければならない(SHALL)。切断後に発行される新規の LETHE 往復は有界(理想的にはゼロ、最悪でも進行中ページ境界までの有限回)でなければならない(SHALL)。

切断されたトレース処理の滞留によって、共有スレッドプールが枯渇し他エンドポイントが無応答化する事態を起こしてはならない(SHALL NOT)。

#### Scenario: 切断後に台帳走査が停止する
- **GIVEN** 監査トレース要求が台帳を読み進めている最中
- **WHEN** クライアントが切断(またはタイムアウト)する
- **THEN** サーバは切断を検知し、以降の新規台帳読み取り往復を発行せずに当該処理を速やかに終える

#### Scenario: 切断の繰り返しでスレッドプールが枯渇しない
- **GIVEN** 監査トレース要求が繰り返し発行され、その都度クライアントが完了前に切断する
- **WHEN** 切断が多数回続く
- **THEN** 切断済み処理が滞留してスレッドプールを枯渇させることはなく、他のエンドポイントは応答性を保つ

### Requirement: ATA-03 単一トレースの応答時間(本番相当データ量)

システムは、Operational Ledger が本番相当規模(5 万イベント)であっても、単一の監査トレース取得(`trace_notification` / `trace_execution`)を数秒台で完了させなければならない(SHALL)。トレース取得のために台帳を cursor 0 から末尾まで全走査(台帳規模 N に対して O(N) の読み取り)してはならない(SHALL NOT)。代わりに、対象に関連するイベントだけを取得する読み取り(ストリーム読み、ローカル派生索引、または LETHE 側の絞り込み取得)を用いなければならない(SHALL)。応答時間の閾値(SLO)は設定値として定義しなければならない(SHALL)。

#### Scenario: 5 万イベント台帳で単一トレースが数秒台で完了する
- **GIVEN** 約 5 万イベントを含む Operational Ledger と、有効な execution_id
- **WHEN** `GET /api/audit-traces/executions/{id}` を単発で呼び出す
- **THEN** 応答は設定した数秒台の SLO 内で返る(現状の 3〜6 分に対する改善)

#### Scenario: 台帳の全走査を行わない
- **GIVEN** 対象トレースに関連するイベントが台帳全体のごく一部である
- **WHEN** トレースが対象イベントを取得する
- **THEN** cursor 0 からの全件走査は行われず、対象に関連するイベントだけを取得する読み取りが用いられる

#### Scenario: 台帳規模の増加に応答時間が比例しない
- **GIVEN** 台帳規模が 5 万から更に増加した状態
- **WHEN** 同一対象の単一トレースを取得する
- **THEN** 応答時間は台帳規模 N に線形比例せず、対象関連イベント件数に応じた時間で収まる

### Requirement: ATA-04 トレース結果の正しさ(改善前後一致)

システムは、改善後のトレース出力が、改善前の全走査実装(canonical)と同一入力に対して同一の結果を返すことを保証しなければならない(SHALL)。同一とは、帰属(recipient_agent_name / agent_name)、delivery / assignment / receipt の各フィールド、timeline、`verified` 判定が一致することを指す。全走査実装が担保していた不変条件検証——配送イベントが厳密に 1 件、昇格イベントが高々 1 件、名前割当と receipt が各 1 件、cursor の連続性、台帳 payload とプロジェクションの一致、帰属名の突合——を維持しなければならず(SHALL)、違反時は改善前と同じく `InvariantViolation` を送出しなければならない(SHALL)。

#### Scenario: 新旧実装の出力が一致する
- **GIVEN** 同一の Operational Ledger 状態と同一の notification_id / execution_id
- **WHEN** 全走査(canonical)実装と改善後実装の双方でトレースを取得する
- **THEN** 両者の帰属・timeline・delivery/receipt フィールド・`verified` がすべて一致する

#### Scenario: 不変条件違反が改善後も検出される
- **GIVEN** 配送イベントが欠落している、または昇格が複数存在する等の不整合な台帳状態
- **WHEN** 改善後実装でトレースを取得する
- **THEN** 改善前と同一の `InvariantViolation` が送出され、不整合が黙って通過しない

#### Scenario: ローカル派生索引は台帳から決定的に再構築できる
- **GIVEN** 高速化のためのローカル派生索引 / キャッシュを用いる実装
- **WHEN** 索引が破損または不在である
- **THEN** 索引は真実ではなく派生として扱われ、canonical な Operational Ledger から決定的に再構築でき、再構築後のトレース結果は全走査実装と一致する

### Requirement: ATA-05 既存契約・テストの非破壊

システムは監査トレース API の wire 契約——レスポンス JSON の形状、HTTP ステータス、認可(`Depends(authorize)`)——を維持しなければならない(SHALL)。`AuditTraceService` の公開インターフェース(`trace_notification` / `trace_execution` / `trace_reply` メソッドおよび `trace_notification_delivery` / `trace_execution_attribution` / `trace_reply_chain` モジュール関数)の外形と戻り値の意味を維持しなければならず(SHALL)、監査トレースが Operational Ledger を読み取るのみで監査イベントを追記しない read-only 原則を維持しなければならない(SHALL)。本 change は既存テストを壊してはならない(SHALL NOT)。

#### Scenario: wire 契約が維持される
- **GIVEN** 監査トレース API の既存クライアント
- **WHEN** 改善後の API を呼び出す
- **THEN** レスポンス形状・ステータス・認可要件は変わらず、既存クライアントは改修不要で動作する

#### Scenario: 公開インターフェースと read-only 原則が維持される
- **GIVEN** `AuditTraceService` を利用する既存コードとテスト
- **WHEN** 改善後の実装に差し替える
- **THEN** 公開メソッド / 関数の外形と戻り値の意味は保たれ、トレースは台帳へ監査イベントを追記せず(read-only)、既存テストは無改変で通る
