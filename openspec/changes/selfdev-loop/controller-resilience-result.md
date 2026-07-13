# selfdev controller resilience 修正報告

## 実施日

2026-07-13

## インシデントの真因

今回の `TimeoutError` は、Proposalの active wall-clock budget を見る
implementation Run timerではなく、Codex backend の単発呼び出しタイマーが発火したものと特定した。

- `vsm.toml` の Codex backend は `timeout_seconds = 900`（15分）だった。
- `CodexRuntime.invoke()` は `process.communicate()` を `asyncio.wait_for()` で囲み、request指定がなければ
  backendの `timeout_seconds` を使う。
- 旧selfdev controllerにはProposalの `budget_estimate.active_wall_clock_seconds` に基づくRun全体の外側timerがなく、
  backendが投げた素の `TimeoutError` の `str(exc)` をそのままABORT reasonへ渡していた。
- S1がworktreeへ `candidate.patch` 相当の変更を作った後でも単発呼び出しが完了しなければ上記timerは発火するため、
  「成果があるのにTimeoutError」という経過と一致する。
- cleanupのsnapshot自体は先に実行されていたが、空reasonの `proposal_state_changed` がstrict schemaの
  `min_length=1` に違反し、`run_forever()`まで例外が伝播してcontroller taskがfatal化した。

## 修正内容

### reasonの空文字防止

- `vsm/selfdev/reasons.py` に非空reasonの正規化を追加。
- `proposal_state_changed`、ABORT、pause、`tool_failed`、Consortium abortの発行経路で、空の例外文字列を
  `例外型名 (文脈)`へ変換する。
- タイムアウトは次のように区別して記録する。
  - `backend invocation timer (<秒> seconds) expired`
  - `implementation run timer (<秒> seconds) expired`

schemaの `reason: Field(min_length=1)` は変更していない。

### Proposal単位の障害隔離

- `SelfDevController.step()` にProposal処理の例外境界を追加。
- Proposal処理中の通常例外は、当該Proposalの algedonic pain／Human notification とABORTへ収束し、
  controller taskへ伝播させない。
- ABORT cleanupでpatch保存またはworktree処理に失敗した場合は、当該Proposalへrecovery pauseを残して継続する。
- Event Logの破損、durable append失敗、起動時reconcile失敗など、controller自身が正本を安全に継続できない場合は、
  従来どおりfatalとして扱う。

### implementation Run timeout

- Run全体の許容時間を次式で導出する。

  `ProposalManifest.budget_estimate.active_wall_clock_seconds + SelfDevConfig.implementation_timeout_margin_seconds`

- 余裕の既定値は300秒。TOMLでは `[selfdev] implementation_timeout_margin_seconds` で設定できる。
- backend単発呼び出しの `timeout_seconds` は別タイマーとして扱い、`BackendInvocationTimeout` として診断する。
- implementation Runとrepair Runの双方に外側timerを適用する。

### timeout後の成果物

v1の失敗マトリクスどおり、timeout後の状態は `ABORTED` のままとした。workspaceが存在する場合は、既存の
cleanup順序を維持し、次を保存する。

- `artifacts/candidate.patch`
- workspace audit/status/diff情報
- implementation effectの `tool_invoked`／`tool_failed` とABORT／algedonicイベント

timeout後に `GATES_RUNNING` へ進める設計は、v1では採用していない。

## テスト結果

変更対象ツリーをマウントした一時Docker Compose `app` コンテナで実行した。
常駐中の既存appの再起動・停止は行っていない。

- `python -m compileall -q vsm tests` — 成功
- `python -m pytest` — **451 passed**, 1 warning（全体実行時点）
- 最終追加のbackend単発タイマー診断を含むフォーカス試験 — **4 passed**
- 追加テスト — 空TimeoutErrorのreason／patch保存、controller health維持と次Proposal処理、timeout導出とTOML設定反映

なお、標準入口の `/home/user/projects/Nanihold_OS` にある既存常駐appはWindows側作業ツリーと内容が一致せず、
`vsm/selfdev` が存在しなかった。そのため今回の検証は対象WindowsツリーをComposeへマウントした一時コンテナで行った。

## 変更ファイル

- `vsm/selfdev/controller.py`
- `vsm/selfdev/effects.py`
- `vsm/selfdev/consortium_adapter.py`
- `vsm/selfdev/reasons.py`
- `vsm/config.py`
- `vsm/web/selfdev_runtime.py`
- `tests/unit/test_selfdev_wave3.py`
- `tests/unit/test_config.py`
- `openspec/changes/selfdev-loop/design.md`
- `README.md`

git commitは作成していない。
