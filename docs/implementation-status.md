# Implementation status

基準日: 2026-07-20

## 実装済み

- Run directory と lifecycle に依存しない DataSpace / UVSMNode / WorkItem / Execution / Event Kernel
- 再帰的 resident S1–S5/S3*、Work Graph、局所介入、重大 finding の S5 acceptance
- Effect planning、Lease、unknown reconciliation、BudgetReservation
- LETHE OperationalEventStore adapter と Projection rebuild
- owner message の先行保存、一回の structured Interface response、model-free status
- Claude mode 隔離、sandbox 証明、requested/actual model mismatch
- PilotHost identity、cursor、ack、切断 pause、WebSocket stream
- exact ModelCandidate registry、public prior、Bayesian posterior、三目的 score
- verified routing outcome と Token Lab baseline/observation の Event 永続化
- S3* → owner の RouteSnapshot approval と evidence cursor freshness
- Token Efficiency Lab の即時／20件 gate、Fable/Opus 禁止
- resource REST API、Event cursor、WebUI control plane
- ownership fail-fast の一回限り migration、digest archive
- 個人情報を commit しない 119 owner-input golden manifest

## 検証

- Kernel、Pilot、routing、Interface、Token Lab、migration、API は Fake Ledger/Pilot/Clock と固定値で試験する。
- golden replay は raw owner text を含めず、local/LETHE source と SHA-256 で結び付ける。
- live Fable/Opus test は行わない。
- frontend は TypeScript production build を要求する。
- LETHE backend conformance は LETHE repository 側で SQLite/PostgreSQL 共通試験を行う。

検証結果:

- Nanihold Fake/logic/API/migration/golden: 22/22 PASS
- UX golden: 119/119 PASS
- Python compileとCLI surface: PASS
- WebUI TypeScript production build: PASS（1580 modules、JS 216.62kB）
- 独立deterministic S3* gate: PASS（旧surface、API、UX、privacy invariant）
- live Fable/Opus call: 0

legacy scan:

- 540 files、4,295,099 bytes、24 event logs
- import候補944 records
- manifest SHA-256 `4fedffaab3ddff9c00267e64369ab051e1c484d202832451f3b0363d012af5d3`
- 15 sourceの所有先が未確定のため、仕様どおりdry-run/importは停止

production traffic の 50%/70% token 削減率は未測定であり、合格扱いにしません。

## 統合状況

- Nanihold feature commit: `0883d7c`
- Nanihold `main` integration merge: `0b97968`
- LETHE feature commit: `b66ceae`
- LETHE `master` integration merge: `046464d`
- 2026-07-20、独立gate通過後に両integration branchの公開remote pushを確認
- production deploy: 対象外、未実施
