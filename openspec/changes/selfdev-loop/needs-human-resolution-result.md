# NEEDS_HUMAN integrity隔離 解決経路の修正結果

## 対象

41c48a0 の Proposal 単位 integrity quarantine 後、active slot を塞いだ
`proposal-e591ebe225714b05a64207ff38ff1a8c` を、通常の Consortium Human waiter
へ誤配送せずに解決できるようにした。

## 原因

- integrity marker 後の Proposal は projection 上 `NEEDS_HUMAN` だが、controller の自動処理は integrity failure を停止条件としていた。
- `human-decision` は integrity隔離かどうかを見ず、initial Consortium の `review-{consortium_id}` waiter へ応答を記録していた。
- `abort` は `_cleanup_workspace()` を先に実行し、隔離対象の `workspace-state.json` が欠損していると recovery pause へ戻るだけで、`ABORTED` state event を記録できなかった。

## 修正

- `proposal_integrity_resolved` を追加し、対象の `proposal_integrity_failed` event に束縛した解決を durable に記録する。
- integrity隔離中の `human-decision` は通常 waiter へ渡さない。`reject` は artifact を変更せず `ABORTED`、`approve` は明示解決後に `APPROVED` へ遷移する。`respond` は 4xx で拒否する。
- integrity隔離中の `control abort` は cleanup を解決条件にせず、`proposal_integrity_resolved(abort)` と `ABORTED` を記録する。これにより cleanup state 欠損でも active slot を解放する。
- 通常の Consortium Human 応答は、現在の `human_review_requested` waiter の存在を確認してから配送する。stale waiter の暗黙利用はしない。
- mutation response に `state`、`state_version`、`event_id`（該当時）、`transition_event_id`（該当時）を追加した。詳細projectionには integrity 解決状態と `pending_action=integrity_resolution` を追加した。

## fixture再現結果

実物 Proposal の manifest / `workspace.json` / 登録 hash fixture に、過去の initial Consortium decision と stale Human waiter を加えて再現した。

- reject: `human_review_responded` は追加されず、`proposal_integrity_resolved(decision=reject)` と `ABORTED` が記録された。
- abort: cleanup state を欠損させたままでも `ABORTED` へ遷移し、active slot 解放後の新規 Proposal 作成が成功した。
- fixture artifact bytes は解決中に書き換えていない。

実運用の Event Log に対する reject / abort は、制約どおり実行していない。

## 検証

Docker Compose の使い捨て `app` test container（本体 Windows tree を `/workspace` に bind）で実行した。

- fixture quarantine（reject / abort / approve 再現を含む）: **6 passed**
- selfdev API: **4 passed**
- Wave 1/2: **19 passed**
- Wave 3（既存の timing-sensitive failure test を除外）: **14 passed**
- resume / runtime deployment: **5 passed**
- API + fixture + Wave 3 回帰（同じ既存1件を除外）: **23 passed, 1 deselected**

最終 full pytest は **456 passed, 2 failed, 1 warning**。失敗は次の既存 timing-sensitive テストだけで、いずれも単独再実行でも同じ結果になった。

- `tests/unit/test_chat.py::test_chat_session_two_turns_restore_and_reject_busy`: busy中の2回目リクエストが 409 ではなく 200 になった。
- `tests/unit/test_selfdev_wave3.py::test_proposal_failure_does_not_kill_controller_and_next_proposal_runs`: 既存の 200 回 × 1ms 待機窓内で `CONSORTIUM_REVIEW` に留まった。

integrity/API/fixture 関連を含む他テストは通過した。上記2件の実装・テストは今回の変更対象外であり、変更差分もないため、本報告では修正せず対象テストと結果を記録する。

なお、AGENTS.md の WSL path `/home/user/projects/Nanihold_OS` は本体とは別の古い checkout で対象 selfdev tree が存在しなかったため、検証だけは同じ Docker Compose 定義を `/mnt/d/userdata/docs/projects/Nanihold_OS` から起動して行った。既存起動サービスや実運用データは操作していない。
