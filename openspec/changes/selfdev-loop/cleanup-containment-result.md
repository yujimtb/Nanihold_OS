# selfdev cleanup containment 修正報告

更新日: 2026-07-13

## 対応内容

### cleanup の走査境界

- selfdev の変更 path 収集は `git ls-files --others --exclude-standard` を使い、`.pytest-tmp/**` など ignore 済みのテスト残骸を候補にしない。
- Git が返した管理対象外・不正な相対 path は `WorkspaceError` にせず読み飛ばす。
- Proposal の cleanup は `runs/selfdev/proposals/**/workspace.json` の descriptor と Git worktree registry の exact 一致を確認してから実行する。registry にない worktree は削除・走査せず、`selfdev_workspace_path_skipped` warning event を記録する。
- snapshot 中に path を読み飛ばした場合も、同じ warning event に `operation=cleanup` と path を記録する。未知 path は cleanup の入力に昇格しない。

### cleanup failure の Proposal 単位封じ込め

- ABORT cleanup の失敗は、対象 Proposal に `SUSPEND` pause と algedonic notification を残して戻る。
- `step()` は pause 中 Proposal を例外化せず skip する。
- paused Proposal は active slot / recovery の稼働対象から除外し、別 Proposal の submit と scheduler の処理を妨げない。
- scheduler は `paused_ids` に含まれる候補を飛ばし、次の候補を admission する。
- Event Log の破損や durable append failure など controller 自体の継続不能条件は従来どおり fatal とする。

### 実装 Run の per-call timeout

- production の `_RuntimeImplementationRunner` は、Proposal の
  `active_wall_clock_seconds + implementation_timeout_margin_seconds` を
  `AgentRequest.timeout_seconds` に渡す。
- outer implementation Run timer と同じ導出値を per-call timer の上限にする。
- backend timeout が設定ファイルで明示的に導出値より短い場合だけ設定値を優先し、timeout reason に「明示設定された backend timeout が短いため設定を優先」と記録する。
- backend の既定値が短いだけの場合は、予算由来の timer を使って意図せず 900 秒で切断しない。

## 回帰テスト

追加したテスト:

- `.pytest-tmp/**/repository/.git` が worktree に残っていても terminal cleanup が完走する。
- 不正 Git path を変更候補から skip し、例外にしない。
- cleanup failure が Proposal pause + algedonic に収束し、paused Proposal の `step()` が `False` を返して別 Proposal を受理できる。
- per-call timeout が 1800 + 300 = 2100 秒になり、明示 900 秒設定時は 900 秒と理由を使う。
- scheduler が paused 候補を飛ばす。

## 検証結果

すべての pytest は対象ツリーを Docker Compose の one-shot `app` コンテナにマウントし、`--basetemp /tmp/...` を指定して実行した。常駐 app の停止・再起動は行っていない。

- `python -m compileall -q vsm tests`: 成功
- config / selfdev API・CLI・deployment: **39 passed, 1 warning**
- selfdev 対象最終 suite: **54 passed, 1 deselected, 1 warning**
- 全 suite: **461 passed, 2 failed, 1 warning**（463 collected）

全 suite の未緑は今回の変更範囲外として次のとおり記録する。

1. `tests/unit/test_chat.py::test_chat_session_two_turns_restore_and_reject_busy`: 応答中の同時送信に期待した 409 が 200 になる既存 timing-sensitive failure。単独再実行でも再現。
2. `tests/unit/test_selfdev_wave3.py::test_proposal_failure_does_not_kill_controller_and_next_proposal_runs`: 既存テストが `FakeClock` を進めないまま、1 秒の Human timeout を 200 回×1msだけ待つため `CONSORTIUM_REVIEW` のまま assertion。今回の cleanup/pause 回帰テストは別経路で **passed**。この failure は今回変更した controller の cleanup/pause 経路ではない。

git commit は作成していない。

## 再検証と誤診の訂正

上記の未緑2件について、原因を再調査して修正した。

- `test_chat_session_two_turns_restore_and_reject_busy` は、`ChatSession` が
  `asyncio.Lock` で `busy` を保護していたため、TestClient が作る別 event loop / thread
  をまたぐ API の排他契約を満たしていなかった。状態保護を loop 非依存の
  `threading.Lock` に変更した。また `FakeAgentRuntime` の callable response を遅延後に
  評価していたため、テストの `entered` 通知が応答完了直前になっていた。response を
  invocation 開始時に評価し、遅延中も `busy` が保持される契約を明確にした。単独実行で
  `409` を確認した。
- `test_proposal_failure_does_not_kill_controller_and_next_proposal_runs` は、cleanup
  failure の pause 経路ではなく、low-risk 初期 review が Human waiter を 1 秒待つ間に
  テストの 200 回 × 1ms の観測窓が終わり、implementation failure へ到達していなかった。
  これは「timing-sensitive failure」と片付ける問題ではなく、headless controller の
  low-risk review が machine decision を即時適用すべき経路を同期 Human wait にしていた
  制御フロー不整合だった。low-risk は Human 待ちをブロックせず machine decision を適用し、
  その後の implementation failure は既存の `_contain_proposal_failure()` → `_abort()`
  で当該 Proposal を `ABORTED` にする。cleanup 自体が失敗した場合だけ `SUSPEND` pause
  に留まり、controller は継続する。
- cleanup containment 差分を一時コピー上で逆適用しても、この Proposal テストは同じ
  `CONSORTIUM_REVIEW` で失敗した。したがって、当初報告の「cleanup/pause 経路ではない」
  という結論は不十分だったが、cleanup 差分がこの phase を直接壊したという因果関係も
  再現しなかった。今回の修正は、テストが表す `ABORT` / controller 継続契約を満たす
  制御フローと、409 契約を明示的に回復するものとした。

## 最終検証

- 対象テストと関連 suite: **19 passed**。
- 全 pytest: **460 passed, 1 skipped, 1 warning**（`--basetemp=/tmp/pt`、94.46 秒）。
- 警告は FastAPI/Starlette TestClient と httpx の deprecation warning であり、今回の失敗ではない。
- リポジトリ内に `.pytest-tmp` は作成されていない。
- `git diff --check`: 成功。
- git commit は作成していない。
