# 自己開発ループ Wave 3

Wave 3 は Wave 2 の Proposal workspace / GateRunner v2 / candidate commit を headless controller へ接続し、提案から `MERGE_READY` までを一周させる。REST API、CLI、WebUI、frontend は Wave 4 の責務として実装していない。

## 永続契約

- Proposal は `runs/selfdev/proposals/<proposal_id>/proposal.json` に immutable な `ProposalManifest` として保存する。
- controller の Event Log は `runs/selfdev/controller/events.jsonl` に集約し、`selfdev:proposal:<proposal_id>` stream を使う。
- Proposal の主状態は `ProposalPhase`、休止は `PauseKind.SUSPEND` / `PauseKind.QUOTA_WAIT` の直交集合で表す。
- 自己開発 Event Store は `durability="durable"` と strict recovery を明示し、append 完了時点で fsync 済みの Event を返す。
- Proposal projection は Event Log から再構成でき、`projection.json` は正本ではない。

## 実装入口

`vsm.selfdev.models` が manifest・gate/audit/PR data model、`state_machine` が遷移と pause、`events` が version 1 payload、`store` が durable stream、`artifacts` が atomic write/hash、`ready_queue` が依存・scope・quota の純粋判定を提供する。

`RunManifest` は新契約では Proposal と Run を分離し、branch を `selfdev/<proposal_id>` から導出する。旧 runtime の既存テストで使われる legacy constructor は残している。

## Wave 2 の実装入口

- `vsm.selfdev.workspace`: `create` / `adopt_existing` / `snapshot` / `finalize` と Proposal workspace descriptor。
- `vsm.selfdev.verification`: scope、protected approval hash、固定 `g1..g4` の検証。
- `vsm.selfdev.git`: controller 限定の diff digest と `CandidateCommitter`。push/merge は提供しない。
- `vsm.gates.runner`: controller-owned 出力先を要求する GateReport v2。適用外 `skip` と実行不能 `error` を分離する。

Platform は Proposal Run の worktree を借用するだけで、`shutdown()` では削除しない。terminal または `MERGE_READY` の cleanup は Proposal workspace controller が行う。

## Wave 3 の実装入口

- `vsm.selfdev.controller`: `start` / `step` / `run_once` / `run_forever`、Proposal 状態遷移、workspace、実装/repair Run、Gates、commit、audit、final Consortium、cleanup。
- `vsm.selfdev.consortium_adapter`: S3/S4/S5 固定順・2 round の dossier-aware 合議、strict synthesis、risk 別 Human timeout、protected approval。
- `vsm.selfdev.effects`: `tool_invoked → side effect → artifact → tool_completed` journal。未完了副作用は再実行せず停止する。
- `vsm.selfdev.audit`: S3★独立 session による typed `audit_report`。negative verdict は失敗ではなく final Consortium へ提出できる。
- `vsm.selfdev.scheduler`: dependency / MERGE_READY scope conflict / 1.3×quota+reserve の直列 admission。
- `vsm.selfdev.recovery` / `service`: controller lock、strict Event Log/artifact reconcile、process-local headless task。FastAPI lifespan 接続は Wave 4 で行う。

Controller は candidate branch への commit までを担当する。push、PR 作成、merge は呼び出さず、Human outcome は `record_merge_outcome` で明示的に記録する。

## 検証

標準検証は次の Docker Compose one-shot コマンドで行う。

```text
docker compose run --rm --no-deps -u root app sh -c "python -m pip install -q -e '.[dev]' && python -m pytest -q"
```

Windows 側で `docker compose` サブコマンドが利用できない環境では、同じ command を WSL の `/mnt/d/.../wt-loop1` から実行する。
