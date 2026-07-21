# Implementation status

基準日: 2026-07-20

## 実装済み

- Run directory と lifecycle に依存しない DataSpace / UVSMNode / WorkItem / Execution / Event Kernel
- 再帰的 resident S1–S5/S3*、Work Graph、局所介入、重大 finding の S5 acceptance
- Effect planning、Lease、unknown reconciliation、BudgetReservation
- LETHE OperationalEventStore adapter と Projection rebuild
- owner message の先行保存、一回の structured Interface response、model-free status
- Claude mode 隔離、sandbox 証明、requested/actual model mismatch
- PilotHost identity、cursor、ack、切断 pause、Kernel側WebSocket control stream
- exact ModelCandidate registry、public prior、Bayesian posterior、三目的 score
- verified routing outcome と Token Lab baseline/observation の Event 永続化
- S3* → owner の RouteSnapshot approval と evidence cursor freshness
- Token Efficiency Lab の即時／20件 gate、cheap exact candidate allowlist／`low`制約
- resource REST API、Event cursor、WebUI control plane
- ownership fail-fast の一回限り migration、digest archive
- 個人情報を commit しない 119 owner-input golden manifest
- Docker Desktop上の隔離local verification、動的port/secret、LETHE commissioning、外部Claude PilotHost
- local verificationのcheap exact candidate allowlist／`low / observe_only / tools disabled`強制
- Interface turnごとのactual model、input/cache/output token、費用、durationのEvent永続化とWebUI表示
- 厳密なHistoryImportReceiptとInterface Node activation状態機械
- LETHE専用HistoryReader、size制限付きresult JSON、raw blob監査、Interface Pilot drill-down loop
- citation／session coverage／現況のReorientationAssessment gate
- owner承認前のExecution・Effect fail-closed、Effect approval
- canonical Conversation、SurfaceBinding、PilotSession、action receipt reconciliation
- 型付きInterfaceActionからWorkItem、dependency、委任、decision、commitment、Effectをmaterialize
- dependency-aware dispatcherとpublished Bayesian RouteSnapshotの実dispatch接続
- RouteSnapshotの明示retirement、同一route二重publish拒否、後継切替とroute廃止の
  固定理由Event、retired snapshotの非routable表示
- Projection起動cursor以降だけを送る排他的なdispatch event deltaと、
  ACTIVE後の単一WorkItem明示dispatch API
- one-time owner bootstrap、hash永続化、HttpOnly SameSite=Strict cookie
- 認証済み管理deviceからallowed Interface origin専用の短寿命bootstrap codeを発行する
  Web API。ユーザーへBearer tokenやterminal操作を要求しない
- root/fork ProviderSessionと期待費用に基づくcache warming純ロジック
- Interface usageからTokenObservationを自動生成
- 7 source固定のLETHE activation handoff、current Work Graph先行取込、retry可能な再オリエンテーション
- history handoff時に既存canonical Conversationを明示固定し、再オリエンテーションの
  Assessmentが別Conversationへ逸脱することをfail-fastで拒否
- owner approvalもAssessmentのcanonical Conversationに固定し、別surfaceへの訂正・
  承認記録を拒否
- reorientation PilotHost RPCへ監査済みhistory resultとexact Assessment contractを渡し、
  Interface Pilotがinventory、Conversation、session、commitment、WorkItem、citation cursorを
  推測せずにAssessmentを構成する
- LETHEのopen commitment全pageをreorientation開始時にcanonical Conversationへ
  materializeし、activation gateと後続resume packの正本を一致させる
- 832 sessionのような大きなindexをreorientation promptへ列挙せず、初回のsession
  index ref/countと後続turnのcontract digestだけで渡す。最終Assessmentは同じref/countと
  `list_sessions`全page走査をKernelで検証する
- 初回turnの完全なAssessment契約と、resume turnのcompact referenceを分離する。
  compact referenceはcontract digest、session index ref/count、open commitment ID、
  実在WorkItemのID・title・description・acceptance・state、最小history cursorだけを含む
- provider session checkpointをresponse受信直後にEventへ記録し、後続gateで失敗しても
  同じroot sessionへcompact referenceとevent deltaだけを渡して再開する
- `AWAITING_OWNER_CONFIRMATION`のAssessmentに不備がある場合、ownerが理由コード付きで
  `REORIENTATION_ONLY`へ戻せるrevision API。以前のAssessment IDと理由をEventへ残し、
  checkpointとusageを保持したままAssessmentだけを破棄する
- 実在WorkItemがあるのにresume対象が空のAssessmentを決定論的に拒否するactivation gate
- owner訂正とresume対象を無変更で検査してから承認・実WorkItem dispatchするactivation gate
- `vsm tui`、`vsm reorientation start`、`vsm reorientation approve`による内部IDを要求しないowner操作
- production PilotHostのtyped MCP allowlist、generic Interface Pilotの
  `model_selection=provider_configured / effort=high`、Codex exact model/effort、
  provider receipt reconciliation
- model-free `/health/live`、`/health/ready`
- Intercomのcanonical Conversation API cutover clientとloss/duplicate検証
- PC内HAの入力・secret・receipt・VM/k3s/backup/failover静的契約
- 長いInterface/Reorientation payloadをcontent-addressed request documentへ保存し、
  CLIのstdinはdigestを指す256 bytes以下の短い指示だけに限定する。stdout/stderrも
  provider I/O documentへcaptureし、terminalへの長文連続出力を運用経路にしない

## 検証

- Kernel、Pilot、routing、Interface、Token Lab、migration、API は Fake Ledger/Pilot/Clock と固定値で試験する。
- golden replay は raw owner text を含めず、local/LETHE source と SHA-256 で結び付ける。
- live provider test は行わず、cheap exact candidate allowlist／`low`または固定mockだけを使う。
- frontend は TypeScript production build を要求する。
- LETHE backend conformance は LETHE repository 側で SQLite/PostgreSQL 共通試験を行う。

既知の検証結果:

- LETHE `cargo test --workspace`: PASS。外部PostgreSQL実サーバーを要する2 testだけ明示ignored
- LETHE `cargo fmt --all -- --check`: PASS
- Intercom: 172 passed
- WebUI TypeScript production build: PASS（1580 modules、JS約231.01kB）
- Nanihold最終差分全体: 121 passed、3 skipped。RouteSnapshot retirement、
  prior-only route bootstrap、AI Judge単独昇格拒否、bounded dispatch delta、
  ACTIVE後の明示dispatchを含む
- Windows PilotHost launcher: 3 passed。親`PATH`保持、認証付きready、
  2秒後のprocess生存、長いstderr非転送を検証

履歴取込前のlegacy scan:

- 540 files、4,295,099 bytes、24 event logs
- import候補944 records
- manifest SHA-256 `4fedffaab3ddff9c00267e64369ab051e1c484d202832451f3b0363d012af5d3`
- 2026-07-20、ownerが15 sourceすべてをPersonalへ割り当てた
- 正本assignmentはworkspace外部cutover artifact
  `_cutover_20260720_fable_activation/ownership-assignment.json`
- 上記cutover path名の`fable`は当時の暫定呼称を含む履歴上のartifact名であり、
  現行の人格名・役割名・モデル設定ではない
- assignment file SHA-256
  `8c95359d0ee4c2ac64720a4412cf9b0ea1891cae4304104b4c7286d723fa83d8`
- `space:personal-primary`、`owner:primary`、`node:owner-interface`へ15/15を
  過不足なく割り当て、旧sourceごとに15個のConversationを維持した
- 旧S1–S5内部senderを人間発言へ誤帰属しないため、推測による
  `owner_senders`指定は行っていない
- このscan記録時点ではDocker上のproduction migration codeによる再dry-runは
  実行環境gate待ちだった

このscanは後続の正式なhistory import実績ではない。後続取込では7 source kindを
Personal Lakeへ統合し、次を`HistoryImportReceipt`と照合した。

- 48,432 records
- 832 sessions
- 66,917,236 raw bytes
- manifest SHA-256
  `3a46b071fb52d6ce0557e1055b8bef71cc12a1ffd043b84a0e469d0cdc42b7b7`
- ownership source 15/15をPersonalへ割当

cutover directory名に残る`fable`はimmutable provenanceであり、runtime識別子、
Interfaceの人格名、役割名、model設定へ再利用しない。

## 再オリエンテーション実績

履歴取込後、generic canonical Conversation上で実providerを使った再オリエンテーションを
実行した。不備のある旧Assessmentが生成されるまでのreceipt snapshotは10 receiptsで、
8件がprovider応答まで成功し、2件はusage記録前に失敗した。このsnapshotの全receipt集計は
次の通りである。

| 指標 | 実測 |
|---|---:|
| base input | 18 |
| cache creation input | 191,921 |
| cache read input | 36,927 |
| output | 38,715 |
| reported cost | $5.811277 |
| permission classifier作動 | 0 |
| model substitution | 0 |
| permission rejection | 0 |

このtraceではclassifierとmodel substitutionは消費増加へ寄与していない。主要費用は、
修正前経路での全履歴再送（raw会話本文ではなく、全session索引・contract相当の再投入）と、
失敗後に同じ大きなprefixを再作成したcache creation/readである。`base input`だけを見ると
実消費を過小評価するため、end-to-end比較ではcache creation/readを必ず含める。

一度`AWAITING_OWNER_CONFIRMATION`へ到達したAssessmentは、存在する実WorkItem
`work:interface-effective-activity-start`をresume対象に含めず、未完仕事を再開するという
goalを満たさない。このAssessmentはowner承認してはならない。これを決定論的に拒否するgateと、
理由コード`missing_resume_work_item`で`REORIENTATION_ONLY`へ戻すrevision APIを実装した。
owner revision Eventを記録し、修正版runtimeで同じprovider root sessionを再開した結果、
`assessment:interface-reorientation-20260720-02`が1 additional provider callで受理された。
832 session、15 open commitment、8 citationを検証し、resume対象は実在する
`work:interface-effective-activity-start`と一致した。owner承認前は
`AWAITING_OWNER_CONFIRMATION`、Execution、Effect Lease、BudgetReservation 0を維持した。

2026-07-20のowner起動承認後、同一`route_key`で二重公開されていた旧coding snapshotを
承認済み後継への置換理由で`RETIRED`にし、対応WorkItemも後継もない旧combined routeを
廃止理由で`RETIRED`にした。正本
`route:coding-production-20260720-selection-contract`だけが`PUBLISHED / routable`である。
公開benchmark priorだけの初回routeを「AI Judge evidence alone」と誤分類していた条件は、
verified outcomeが一件以上あり、かつ全件cheap AI Judgeの場合だけ拒否するよう修正した。
独立S3*とowner承認を持つprior-only snapshotはbootstrapできる。

owner承認EventによりActivationは`ACTIVE`へ移行した。最初のExecutionはdispatcherが
cursor 0から48,000件超を再走査して2 MBを超えるrequestを作り、Provider到達前に失敗した。
この経路を削除し、runtime Projection完了cursorをDispatcherへ必須注入した。再dispatchは
3.3秒で受付され、実在WorkItemからcoding Pilot processを開始した。terminal receiptは
300秒後に`ProviderTimeout`となり、Ledger cursor 48632へ
`pilot_execution_receipt_recorded / failed`として保存した。actual modelとusageは未確定で、
成功・完了・token観測として扱わない。固定再試行は行わず、WorkItemは`READY`を維持する。
計画上の「活動開始」閾値である履歴取込、再オリエンテーション、owner confirmation、
最初の実WorkItem開始までは完了したが、このWorkItemのacceptance完了とは扱わない。

corrective receipt単体はbase input 4、cache creation input 45,254、cache read input
38,052、output 10,860、reported cost `$1.486172`だった。request documentは
32,290 bytesで、全session IDの記載は0、完全Assessment contractの再送はなく、
WorkItem summary 1件とopen commitment ID 15件を含むcompact referenceだった。一方で
75件のcurrent-state indexを含み、既存root prefixのcache readも残った。従って
full conversation/session replayは解消したが、初回再オリエンテーションのtoken効率を
合格扱いにはしない。11 receipt累計はbase 22、cache creation 237,175、
cache read 74,979、output 49,575、reported cost `$7.297449`であり、
classifier、model substitution、permission rejectionはいずれも0である。

通常の情報引継ぎはprovider session checkpoint、event delta、compact contract referenceを
使い、全session IDや履歴本文を再送しない。長い契約と結果はdocumentに置き、stdioは
一時的な短い指示だけにする。これはtoken効率だけでなく、terminal描画負荷と監査可能性を
含むInterface UXの不変条件である。

production traffic の50%/70% token削減率は、7日間かつ比較可能WorkItem 20件以上の
条件をまだ満たしておらず、合格扱いにしない。

## 未完了・fail-fast境界

- 最初の実WorkItemはProviderTimeoutで未完了。固定再試行やfalse-completeを行わず、
  WorkItemを分解するか次のrouting判断をowner-visibleに確定する必要がある
- Intercomの継続運用に対する最終drain/cursorと、新Conversation APIへの完全cutoverは
  別途運用gateで確定する
- PostgreSQL実server conformance、NAS backup、RPO 0/RTO 5分のlive測定は未実施
- 現行production PilotHostはHTTP active/standby。計画の外向きresumable streamは未実装
- LETHE HA canary、restore-state、分離projection process、canonical backup image契約は未実装
- したがって`deploy/ha`はruntime capability receiptを検証して
  `RUNTIME_CONTRACT_UNAVAILABLE`で停止し、現時点では配備可能と扱わない
- production deployは未実施。Interface Node activationはlocal production構成で
  `ACTIVE`まで完了

## 統合状況

- Nanihold feature commit: `0883d7c`
- Nanihold `main` integration merge: `0b97968`
- LETHE feature commit: `b66ceae`
- LETHE `master` integration merge: `046464d`
- 現行のNanihold `main`には、Nanihold側のintegration merge `0b97968`に加えて、
  `00687a3`、`7ec3f80`、`ecfdbbb`、`f18984c`の後続変更も含まれる
- 2026-07-20、先行architecture commitについて両integration branchの公開remote pushを確認
- 2026-07-20、local verification拡張を実装・live smoke・再起動復元まで確認
- 2026-07-20、完全稼働・再オリエンテーション差分:
  - Nanihold `00687a3`を`agent/interface-activation`へ公開push
  - LETHE `b4d574f`を`agent/nanihold-history-ingestion`へ公開push
  - Intercom `c468aad`をlocal commit。repositoryにremoteがないため未push
- Nanihold側の上記feature変更は現行`main`へマージ済み。公開push済みであることと
  integration merge済みであることは区別する
- production deploy: 対象外、未実施
