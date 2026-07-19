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
- Token Efficiency Lab の即時／20件 gate、Fable/Opus 禁止
- resource REST API、Event cursor、WebUI control plane
- ownership fail-fast の一回限り migration、digest archive
- 個人情報を commit しない 119 owner-input golden manifest
- Docker Desktop上の隔離local verification、動的port/secret、LETHE commissioning、外部Claude PilotHost
- local verificationの`Haiku / low / observe_only / tools disabled`強制とFable/Opus拒否
- Interface turnごとのactual model、input/cache/output token、費用、durationのEvent永続化とWebUI表示
- 厳密なHistoryImportReceiptとFable activation状態機械
- LETHE専用HistoryReader、size制限付きresult JSON、raw blob監査、Fable drill-down loop
- citation／session coverage／現況のReorientationAssessment gate
- owner承認前のExecution・Effect fail-closed、Effect approval
- canonical Conversation、SurfaceBinding、PilotSession、action receipt reconciliation
- 型付きInterfaceActionからWorkItem、dependency、委任、decision、commitment、Effectをmaterialize
- dependency-aware dispatcherとpublished Bayesian RouteSnapshotの実dispatch接続
- one-time owner bootstrap、hash永続化、HttpOnly SameSite=Strict cookie
- root/fork ProviderSessionと期待費用に基づくcache warming純ロジック
- Interface usageからTokenObservationを自動生成
- 7 source固定のLETHE activation handoff、current Work Graph先行取込、retry可能な再オリエンテーション
- owner訂正とresume対象を無変更で検査してから承認・実WorkItem dispatchするactivation gate
- `vsm tui`、`vsm fable catch-up`、`vsm fable approve`による内部IDを要求しないowner操作
- production PilotHostのtyped MCP allowlist、Fable `claude-fable-5/high`、Codex exact model/effort、
  provider receipt reconciliation
- model-free `/health/live`、`/health/ready`
- Intercomのcanonical Conversation API cutover clientとloss/duplicate検証
- PC内HAの入力・secret・receipt・VM/k3s/backup/failover静的契約

## 検証

- Kernel、Pilot、routing、Interface、Token Lab、migration、API は Fake Ledger/Pilot/Clock と固定値で試験する。
- golden replay は raw owner text を含めず、local/LETHE source と SHA-256 で結び付ける。
- live Fable/Opus test は行わない。
- frontend は TypeScript production build を要求する。
- LETHE backend conformance は LETHE repository 側で SQLite/PostgreSQL 共通試験を行う。

今回の検証結果:

- LETHE `cargo test --workspace`: PASS。PostgreSQL実サーバーを要する2 testだけ明示ignored
- LETHE history/import/storage/selfhost E2E: PASS
- WebUI TypeScript production build: PASS（1580 modules、JS 229.33kB）
- Nanihold `ruff check`: PASS
- Intercom: 161 passed、1 warning
- Claude Code 2.1.215／Codex CLI 0.144.5のCLI境界をread-only確認
- live Fable/Opus call: 0

NaniholdのDocker pytest全件は、今回の最終統合差分後には実行環境の承認利用上限により
再実行できていない。以前のlane試験結果を最終差分の合格へ読み替えない。

legacy scan:

- 540 files、4,295,099 bytes、24 event logs
- import候補944 records
- manifest SHA-256 `4fedffaab3ddff9c00267e64369ab051e1c484d202832451f3b0363d012af5d3`
- 15 sourceの所有先が未確定のため、仕様どおりdry-run/importは停止

production traffic の 50%/70% token 削減率は未測定であり、合格扱いにしません。

## 未完了・fail-fast境界

- 15 legacy sourceの所有先が未確定であるため、実history importは開始していない
- Intercom drain、最終cutover cursor、実system snapshotは未確定
- PostgreSQL実server conformance、NAS backup、RPO 0/RTO 5分のlive測定は未実施
- 現行production PilotHostはHTTP active/standby。計画の外向きresumable streamは未実装
- LETHE HA canary、restore-state、分離projection process、canonical backup image契約は未実装
- したがって`deploy/ha`はruntime capability receiptを検証して
  `RUNTIME_CONTRACT_UNAVAILABLE`で停止し、現時点では配備可能と扱わない
- production deployとFable `ACTIVE`化は未実施

## 統合状況

- Nanihold feature commit: `0883d7c`
- Nanihold `main` integration merge: `0b97968`
- LETHE feature commit: `b66ceae`
- LETHE `master` integration merge: `046464d`
- 2026-07-20、先行architecture commitについて両integration branchの公開remote pushを確認
- 2026-07-20、local verification拡張を実装・live smoke・再起動復元まで確認
- 2026-07-20、完全稼働・Fable再起動差分をfeature branchへ統合中
- production deploy: 対象外、未実施
