# 自己開発ループ Wave 2

Wave 2 は Proposal 所有 workspace、scope-aware な trusted GateRunner v2、候補 commit の境界を提供する。controller、API、CLI、UI、Consortium 駆動は後続 Wave の責務であり、この Wave では実装していない。

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

## 検証

標準検証は次の Docker Compose one-shot コマンドで行う。

```text
docker compose run --rm --no-deps -u root app sh -c "python -m pip install -q -e '.[dev]' && python -m pytest -q"
```

Windows 側で `docker compose` サブコマンドが利用できない環境では、同じ command を WSL の `/mnt/d/.../wt-loop1` から実行する。
