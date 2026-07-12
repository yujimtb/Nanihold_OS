# Wave 3 実装結果 — Headless Controller / Consortium / Audit / Scheduler

更新日: 2026-07-13

## 実装範囲

- `vsm/selfdev/controller.py` に `SelfDevController` の `start` / `step` / `run_once` / `run_forever`、Proposal の正式状態機械接続、workspace create/adopt、implementation / repair Run、Gate attempt 1/2、candidate commit、audit、final Consortium、PR description、`MERGE_READY`、terminal cleanup、Human merge/archive outcome を実装。
- `vsm/selfdev/consortium_adapter.py` に S3_ALLOCATOR → S4_SCANNER → S5_POLICY 固定順・2 round の dossier-aware adapter、strict JSON synthesis、S3/S4/S5 runtime 必須検証、durable Human waiter、low proceed / normal・protected abort の risk policy、protected approval を実装。
- `vsm/selfdev/effects.py` に副作用 journal を実装。`tool_invoked` → side effect → artifact → `tool_completed` を記録し、in-doubt effect は再実行せず SUSPEND + Human notification とする。
- `vsm/selfdev/audit.py` に S1 と session を共有しない S3★ typed audit を実装。証拠欠落はエラー、valid な `verdict=fail` は `FINAL_CONSORTIUM` へ提出する。
- `vsm/selfdev/scheduler.py` に同時1件、DONE dependency、MERGE_READY scope conflict、`1.3 × estimate + reserve <= remaining` の admission を実装。`reporting.py` に Asia/Tokyo 日次 report の JSON/Markdown generator を追加。
- `vsm/selfdev/recovery.py` / `service.py` に process-local controller lock、strict Event Log/manifest/artifact reconcile、headless task wrapper を追加。Wave 4 の FastAPI lifespan から接続できる公開 controller surface を用意した。
- `vsm/selfdev/events.py` に tool effect、Human review request/response の version 2 strict schema を追加し、package 初期化の循環を避けるため Wave 3 公開物は遅延 export とした。
- `tests/unit/test_selfdev_wave3.py` に FakeRuntime E2E、repair 1回制限、protected approval、normal timeout、durable waiter 再起動、Consortium decision 拡張、negative audit、scheduler のテストを追加した。

## 仕様からの逸脱・判断

- FastAPI lifespan、REST API、CLI、WebUI、frontend、push/PR 作成/merge は Wave 4 の範囲外として実装していない。controller は candidate commit までで停止し、Human の merge outcome を明示イベントとして受け取る。
- 現行 Event Log の `artifact_created` 契約は Proposal ID を必須とするため、日次 report generator 自体は JSON/Markdown artifact の生成を担当し、Wave 4 の運用配線で report event を controller task に接続する余地を残した。
- 既存 runtime の `Consortium` は置換せず、selfdev adapter が Node/round/statement/convener の既存抽象に相当する契約を固定して利用する。既存の通常 Consortium と Wave 1/2 の公開契約は変更していない。
- selfdev の ProposalManifest は canonical JSON bytes の hash を正本とするため、`write_proposal_manifest` を改め、末尾改行による hash 不一致を解消した。通常 artifact の JSON は従来どおり atomic write + hash を使用する。
- controller 自身の runtime 配線は callback/protocol injection とし、既存 Web Run の timeout/retry/lifecycle へ混ぜていない。Docker/Compose worker の実運用接続は Wave 4 へ引き継ぐ。

## 検証結果

- 指定コマンド `docker compose run --rm --no-deps -u root app sh -c "python -m pip install -q -e '.[dev]' && python -m pytest -q"` は Windows 側 Docker が Compose サブコマンドを提供せず `unknown flag: --rm` で開始不能だった。
- WSL 側は Compose 2.40.3 を認識したが、指定 one-shot は app image の build / 依存導入が完了せず 126 秒で timeout した。Docker/WSL のサービス修復、起動、停止、既存プロセス操作は行っていない。
- 同じ app 系の既存 Docker image を使い、依存を ephemeral container 内へ導入して `python -m pytest -q` を実行し、`433 passed, 1 skipped, 1 warning`（2026-07-13）を確認した。新規 Wave 3 テストは 8 件全緑、Wave 1 は 5 件、Wave 2 は 14 件全緑だった。
- `python -m compileall -q vsm/selfdev tests/unit/test_selfdev_wave3.py` と Event schema import の Docker 内確認も成功した。

## Wave 4 への引き継ぎ

- FastAPI lifespan 起動時に `SelfDevService` / `SelfDevController` を single worker で常駐させ、health と controller fatal 時の mutation 503 を配線する。
- `submit_proposal`、`respond_human`、`suspend`、`abort`、`resume_quota`、`record_merge_outcome` と projection/artifact の一覧・詳細を REST/CLI/WebUI に公開する。controller business logic は `vsm.web.manager` に複製しない。
- 実環境の AgentRuntime、trusted Gate worker、QuotaMonitor / `mission/selfdev-resume`、daily report timer を注入し、resume session・quota reset・process group の実運用契約を E2E で確認する。
- Wave 3 の headless E2E は push/merge を呼ばず `MERGE_READY` で停止するため、人間の最終 merge と protected path の事前承認 UI/CLI を Wave 4 で提供する。
