# Wave 1 実装結果 — Domain / State / Event / Store

更新日: 2026-07-13

## 実装範囲

- `vsm/selfdev/` を追加し、ProposalManifest、scope/protected classifier、構造化 acceptance criteria、budget、origin、audit report、gate report、PR description renderer を実装。
- ProposalPhase の正式遷移、terminal 拒否、`SUSPEND` と `QUOTA_WAIT` の併存、pause の追加・個別解除、repair attempt の状態情報を実装。
- `proposal_state_changed`、`proposal_pause_changed`、`proposal_run_linked` の strict schema version 1 を追加。既存 `llm_*` v2 と selfdev 用 artifact/consortium v2 の dispatch も登録。
- EventLogWriter に version dispatch、起動時 strict recovery、seq/stream version 復元、compare-and-append、durable mode の fsync 完了待ちを追加。
- `runs/selfdev/controller/events.jsonl`、Proposal projection replay、immutable artifact の atomic write/hash、Proposal→RunManifest mapping、ready-queue の dependency/scope/quota 判定を追加。
- quota-state の version/run_id/pool kind/reset/node list を strict 検証し、既知 quota kind の reset 不明時は推測せずエラーにした。
- `tests/unit/test_selfdev_wave1.py` を追加し、既存テストと合わせて検証した。

## 設計からの逸脱

- 既存テストを維持するため、既存 runtime EventLogWriter の既定 mode は `buffered` のままにした。自己開発 Event Store は `durability="durable"` を明示して設計契約を満たす。
- 既存 Node lifecycle の `NodeStatus.SUSPENDED` と `NodeStatus.QUOTA_WAIT` は、既存テストが identity を要求するため現状 alias を残した。Proposal domain の pause cause は alias ではなく独立した `PauseKind` で実装済み。Node enum の完全分離は既存 lifecycle 呼び出しを一括更新する後続 hardening として引き継ぐ。
- provider が quota 種別を `unknown` として返す既存 synthetic runtime だけ、既存 Wave 2 テストのため reset fallback を残した。`five_hour` / `weekly` および selfdev 接続面では reset 不明を fail-fast とする。
- 既存 Platform が probe 未注入で復帰する契約は維持した。probe 未注入を healthy と扱わない完全な統合は、実 probe の接続面と併せて後続 Wave へ引き継ぐ。

## Wave 2 への引き継ぎ

- controller 本体・FastAPI lifespan task・API/CLI/UI は未実装。
- Proposal 所有 workspace の create/adopt/snapshot/finalize、scope-aware GateRunner v2、candidate commit、artifact の controller 副作用 journal を接続する。
- `RunManifest` の legacy constructor と Node の旧 alias を撤去する場合は、既存 runtime 呼び出しを canonical 契約へ一括移行してから行う。
- quota probe を実環境の明示的な health probe として注入し、`QUOTA_WAIT` の復帰・active wall-clock 会計・controller projection を接続する。

## 検証結果

指定の Docker Compose one-shot pytest を WSL 経由で実行し、`411 passed, 1 skipped`。Windows 側の `docker compose` は compose サブコマンド未提供だったため、サービスを起動せず WSL の Compose 2.40.3 を使用した。
