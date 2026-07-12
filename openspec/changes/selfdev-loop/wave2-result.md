# Wave 2 実装結果 — Workspace / GateRunner v2 / Candidate commit

更新日: 2026-07-13

## 実装範囲

- `vsm/selfdev/workspace.py` に Proposal 所有の workspace descriptor と `create` / `adopt_existing` / `acquire` / `snapshot` / `finalize` を追加した。restart 後は Git worktree registry、Proposal ID、branch、path、base の一致を検証し、snapshot は worktree を削除しない。terminal または `MERGE_READY` の finalize でだけ削除する。
- `vsm/selfdev/git.py` に stage/commit/push/merge のうち controller 限定の candidate commit だけを実装した。commit 前に branch、parent/base、GateReport、scope hash、candidate diff digest を突合し、Proposal ID・base・digest の trailer を付与する。
- `vsm/selfdev/verification.py` に scope 判定、protected approval の event/hash 突合、固定 `g1,g2,g3,g4` 契約を追加した。
- `vsm/gates/policy.py` / `runner.py` を scope-aware に拡張した。scope 内の tracked/untracked は許可し、scope 外は G1 fail とする。protected path は risk、Proposal manifest hash、protected scope hash、approval event が一致した場合だけ許可する。GateReport v2 は worktree 外へ保存し、適用外 `skip` と実行不能 `error` を分離する。
- `gate_report_generated` schema version 2 と recorder を追加し、`RunManifest` の新契約で required gates と scope hash を strict 検証する。
- `Platform.shutdown()` は Proposal Run の worktree を削除せず、gate/audit/commit まで Proposal controller が保持できるようにした。

## Wave 3 以降を実装していない範囲

- headless controller、Consortium adapter、audit、scheduler、API、CLI、WebUI は実装していない。
- push、PR 作成、merge は実装していない。
- frontend には変更を加えていない。

## 設計からの逸脱・判断

- 既存の通常 self-hosting Run と既存テストを壊さないため、legacy `RunManifest` / legacy GateRunner v1 の公開契約は残し、新契約の Proposal Run / GateReport v2 とコード経路を分離した。新契約では暗黙 fallback を行わず、必須 metadata・scope・base・出力先が欠ければ fail-fast する。
- GateRunner v2 の実行時環境は control-plane の `PYTHONPATH` と trusted marker を明示する。Docker Compose の app worker を別プロセスへ分離する controller 配線は Wave 3 以降の責務として残した。

## 検証

- `tests/unit/test_selfdev_wave2.py` を追加し、Proposal workspace の snapshot/terminal cleanup・初回 collision・Proposal worktree の Platform shutdown 保持、GateRunner v2 の scope/未追跡/protected approval/required gates/strict metadata、外部 gate subprocess のモック、Proposal-bound candidate commit を検証対象にした。
- `git diff --check` は成功した。
- 2026-07-13 に指定の Windows 側コマンドを実行したが、環境の Docker が Compose サブコマンドを認識せず `unknown flag: --rm` で pytest 開始前に終了した。Docker 設定ファイルの access denied 警告も発生した。
- WSL 側の現在の worktree から同じ指定コマンドを実行したが、無出力のまま 126 秒でタイムアウトし、`write /dev/stdout: broken pipe` で終了した。pytest の件数・緑結果は取得できなかった。Docker/WSL のサービス修復やプロセス操作は行っていない。
- 上記のため、今回の pytest 全体結果は未確定であり、静的確認（テスト観点・差分整合性・`git diff --check`）までを記録した。実行ゲートは人間側に引き継ぐ。

## Wave 3 への引き継ぎ

- `ProposalWorkspace` の副作用を controller Event Log の `tool_invoked → effect → artifact → tool_completed` journal に接続する。
- GateReport v2 の保存/hash と `CandidateCommitter` の結果を状態機械へ接続し、GATES_FAILED の repair 1回制限、AUDIT、FINAL_CONSORTIUM、terminal cleanup を実装する。
- trusted Gate worker を app 配備環境へ接続し、初回 workspace collision、restart reconcile、quota wait/resume と candidate branch の保持を E2E で検証する。
