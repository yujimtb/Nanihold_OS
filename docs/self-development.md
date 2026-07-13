# 自己開発ループ Wave 4

Wave 4 は Wave 3 の headless controller を REST API、CLI、WebUI、frontend、single-worker 運用へ接続し、提案から `MERGE_READY` までを人間が追跡できる公開面にする。

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

Platform は Proposal Run の worktree を借用するだけで、`shutdown()` では削除しない。terminal または `MERGE_READY` の cleanup は Proposal workspace controller が行う。cleanup の走査対象は `runs/selfdev` の registry と Git worktree registry が exact 一致する path に限定し、未知 path は `selfdev_workspace_path_skipped` warning event を記録して skip する。cleanup failure は Proposal の SUSPEND + algedonic に封じ込める。

## Wave 3 の実装入口

- `vsm.selfdev.controller`: `start` / `step` / `run_once` / `run_forever`、Proposal 状態遷移、workspace、実装/repair Run、Gates、commit、audit、final Consortium、cleanup。
- `vsm.selfdev.consortium_adapter`: S3/S4/S5 固定順・2 round の dossier-aware 合議、strict synthesis、risk 別 Human timeout、protected approval。
- `vsm.selfdev.effects`: `tool_invoked → side effect → artifact → tool_completed` journal。未完了副作用は再実行せず停止し、詳細の in-doubt 効果一覧を Human が completed/failed と理由付きで裁定すると journal と recovery pause を解決する。
- `vsm.selfdev.audit`: S3★独立 session による typed `audit_report`。negative verdict は失敗ではなく final Consortium へ提出できる。
- `vsm.selfdev.scheduler`: dependency / MERGE_READY scope conflict / 1.3×quota+reserve の直列 admission。
- `vsm.selfdev.recovery` / `service`: controller lock、strict Event Log/artifact reconcile、process-local headless task。FastAPI lifespan は `vsm.web.app` から single worker の service を起動・停止する。

Controller は candidate branch への commit までを担当する。push、PR 作成、merge は呼び出さず、Human outcome は `record_merge_outcome` で明示的に記録する。

## Wave 4 の公開面

REST は `/api/selfdev` に分離し、`POST /proposals`、一覧（`state` / `pending_action=human`）、詳細、
SSE events、control、human-decision、in-doubt effect 裁定、merge-outcome、allow-list artifact、health を提供する。作成時の
ID・時刻・actor は controller が付与し、stale `state_version` は 409、manifest/schema は 422、
controller 未配備・fatal・durable append 不能は 503 で返す。

`vsm selfdev` はこの loopback REST のみを呼び、`propose --file`、`list`、`show`、approve / reject /
respond、suspend / resume / abort、outcome を公開する。API 停止時に Event Log writer を直接起動する
経路はない。

WebUI は既存の対話・組織図と同じローカル日本語デザインで自己開発タブを持つ。Proposal一覧、
状態 rail、合議全文、gate / audit / budget、artifact、PR説明文コピー、Human承認、in-doubt 効果の一覧・裁定、介入、merge outcome
を表示する。push / PR作成 / merge 操作は意図的に存在しない。`compose.yaml` の app は `--workers 1`
固定で、`--reload` は selfdev controller の二重起動防止のため使用しない。

## 検証

標準検証は次の Docker Compose one-shot コマンドで行う。

```text
docker compose run --rm --no-deps -u root app sh -c "python -m pip install -q -e '.[dev]' && python -m pytest -q"
```

Windows 側で `docker compose` サブコマンドが利用できない環境では、同じ command を WSL の `/mnt/d/.../wt-loop1` から実行する。
