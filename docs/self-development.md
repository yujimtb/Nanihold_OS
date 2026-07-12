# 自己開発ループ Wave 1

Wave 1 は Proposal の Domain / State / Event / Store 境界を提供する。controller、API、CLI、UI、workspace 操作、GateRunner、Consortium 駆動は後続 Wave の責務であり、この Wave では実装していない。

## 永続契約

- Proposal は `runs/selfdev/proposals/<proposal_id>/proposal.json` に immutable な `ProposalManifest` として保存する。
- controller の Event Log は `runs/selfdev/controller/events.jsonl` に集約し、`selfdev:proposal:<proposal_id>` stream を使う。
- Proposal の主状態は `ProposalPhase`、休止は `PauseKind.SUSPEND` / `PauseKind.QUOTA_WAIT` の直交集合で表す。
- 自己開発 Event Store は `durability="durable"` と strict recovery を明示し、append 完了時点で fsync 済みの Event を返す。
- Proposal projection は Event Log から再構成でき、`projection.json` は正本ではない。

## 実装入口

`vsm.selfdev.models` が manifest・gate/audit/PR data model、`state_machine` が遷移と pause、`events` が version 1 payload、`store` が durable stream、`artifacts` が atomic write/hash、`ready_queue` が依存・scope・quota の純粋判定を提供する。

`RunManifest` は新契約では Proposal と Run を分離し、branch を `selfdev/<proposal_id>` から導出する。旧 runtime の既存テストで使われる legacy constructor は残している。

## 検証

標準検証は次の Docker Compose one-shot コマンドで行う。

```text
docker compose run --rm --no-deps -u root app sh -c "python -m pip install -q -e '.[dev]' && python -m pytest -q"
```

Windows 側で `docker compose` サブコマンドが利用できない環境では、同じ command を WSL の `/mnt/d/.../wt-loop1` から実行する。
