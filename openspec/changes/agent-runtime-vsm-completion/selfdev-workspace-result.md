# selfdev workspace 実装結果

## 実装内容

- `vsm/runtime/manifest.py` を追加した。
  - `RunManifest` は Run ID、repository、base SHA、`selfdev/<run_id>` branch、worktree、変更許可/禁止 path、受入条件、必須 gate、backend/model/budget、risk class、decision/conversation 発行元を保持する。
  - `manifest.persist(run_dir)` / `RunManifest.load(run_dir)` で `manifest.json` を永続化・再読込する。
  - `WorkspaceController` は base SHA から `git worktree add -b` で専用 worktree を作り、終了・中断時に `candidate.patch`、`git-status.txt`、`git-diff-summary.txt`、`workspace-audit.json` を保存してから worktree を削除する。
  - `find_orphan_worktrees` は `git worktree list --porcelain` と manifest の worktree path を照合し、`selfdev/*` の孤児だけを返す。
  - 許可外/禁止 path の変更は成果物を保存・worktree 削除後に `WorkspacePolicyError` として報告する。
- `vsm/runtime/lifecycle.py` で manifest 付き Run の worktree を起動・終了へ接続した。manifest がない通常 Run も `run_dir/workspace` を作成し、稼働 checkout を agent の cwd にしない。
- `vsm/systems/base.py` で全 SubAgent の `AgentRequest.workdir` を必ず Run の束縛先から設定し、束縛先以外への上書きを拒否する。S1 の動的生成にも同じ束縛を適用した。
- S3 の割当時に writer 所有者を検査し、同一 manifest の worktree へ書き込み可能性がある runtime（`litellm` 以外）を持つ S1 を同時に二つ以上割り当てると明確な `WorkspaceError` にする。S1 の正常完了時だけ所有権を解放し、quota 中断中は保持する。
- `vsm/config.py` に `SelfDevConfig` と `[selfdev]` を追加した。`enabled`、repository（既定はカレントディレクトリ）、`forbidden_paths`（既定は `AGENTS.md`、`.github/`、`vsm.toml`、`openspec/`）を厳格に読み込む。`enabled=true` の Run は manifest なしでは開始しない。

## 設計判断

- Agent が stage/commit/push/rebase/merge を行う経路は追加していない。controller は candidate patch と監査情報の収集までを担当し、branch は後続の controller commit が利用できる状態で残す。
- worktree からの差分は `git diff <base_sha>` で収集し、staged と unstaged の tracked 変更に加えて untracked file の `/dev/null` 差分を一つの `candidate.patch` にまとめる。untracked file は status/audit にも残す。
- protected path への変更は patch を失わないよう、監査成果物の保存と worktree 削除を先に完了してから policy error を送出する。
- writer の同時性は S3 の割当境界で検査し、S1 が通常完了した時点で解放する。quota による中断は同じ作業の再開を前提に所有権を保持する。

## テスト

`tests/unit/test_selfdev_workspace.py` に、tmp の実 git repository を使った次の決定論テストを追加した。

- manifest の永続化・再読込
- branch/worktree 作成、FakeRuntime への workdir 伝搬
- tracked diff の candidate patch 保存
- status/diff 概要保存と worktree 掃除
- 許可外変更の fail-fast
- manifest path と git worktree 一覧の照合による孤児検出
- 1 Run 1 writer の競合検出
- `[selfdev]` 設定の読み込み

指定コマンド

```text
docker compose run --rm --no-deps -u root app sh -c "python -m pip install -q -e '.[dev]' && python -m pytest -q"
```

は Windows 側では Docker 設定ファイル `C:\Users\mitob\.docker\config.json` のアクセス拒否により `--rm` を Docker 本体へ誤解釈して起動できなかった。WSL 側の Compose 入口でも同じ指定を再試行したが、Docker/Compose の起動待ちのままタイムアウトした。WSL、Docker サービス、プロセスの修復・操作は制約に従い実施していない。`git diff --check` は成功している。

## smoke_run 回帰の原因と修正

`timeout 30 python scripts/smoke_run.py` を WSL 側の Compose app（`-u root`）で確認したところ、通常 Run は `manifest=None`、`workspace_controller=None` のままで、git/worktree 操作や 1 Run 1 writer の検査は実行していなかった。[selfdev] の既定 `enabled=False` も意図どおりだった。

停止箇所は `s1_completion` 観測後の `platform.shutdown()` だった。S4 が `asyncio.wait()` で待機中にキャンセルされると、`s4_task_get` / `s4_s5_get` の Queue getter 子 task を回収せずに残し、`Platform.shutdown()` 後も `asyncio.run()` がその子 task の終了を待ち続けていた。workdir 解決、WorkspaceController、S1 の writer 割当待ちが原因ではない。

`vsm/systems/s4_scanner.py` で、通常反復時の pending getter と shutdown 時に `asyncio.wait()` 前でキャンセルされた全 getter を `asyncio.gather(..., return_exceptions=True)` で回収するよう修正した。`tests/unit/test_s4_shutdown.py` に、S4 の待機 getter が shutdown 後に残らない回帰テストを追加した。Compose の root 実行で一時 git repository を使えるよう、selfdev テストの子 git プロセスにだけ `safe.directory=*` を環境変数で設定し、システム全体の git 設定は変更していない。

## 再検証結果

- 失敗テスト: `1 passed in 6.74s`
- selfdev 関連テスト: `6 passed in 29.51s`
- 指定の全 pytest: `385 passed, 1 skipped, 1 warning in 114.31s`
- smoke の直接実行: `s1_completion` 観測後に正常 shutdown・`run_dir` 出力まで完了

全 pytest は WSL の Compose 入口で次の指定コマンドを実行して確認した。

```text
wsl --cd /mnt/d/userdata/docs/projects/_mission_20260712_nanihold/wt-selfdev -- docker compose run --rm --no-deps -u root app sh -c "python -m pip install -q -e '.[dev]' && python -m pytest -q"
```

## 残課題

- `GateRunner` は `manifest.required_gates` と `acceptance_criteria` を評価し、`workspace-audit.json` の policy violation を gate failure として扱う接続が必要である。
- 後続 controller は gate 通過後に `manifest.base_sha` を基準に `candidate.patch` を確認し、manifest の branch 上で候補 commit を作成する。commit/push/merge は本変更には含めていない。
