# Trusted GateRunner 実装結果

## 概要

候補 worktree を AI のテスト実行結果から分離して検証する `vsm.gates.runner` を追加した。GateRunner は独立プロセスとして実行され、各ゲートの終了コードと生ログを `gate_report.json` に保存する。AI 側のテスト結果や設定値は merge 判定を上書きできない。

## ゲート仕様

CLI は次の形式で起動する。

```text
python -m vsm.gates.runner --worktree <path> [--base <sha>] [--gates g1,g2,g3,g4] [--out <path>]
```

- G1 は Git の基準差分、未追跡ファイル、symbolic link、`git diff --check`、secret 様の追加内容、`.env` 系追加、変更量を検査する。変更ファイル400超または変更行8000超は警告内容を report に残したうえで拒否する。保護対象は `AGENTS.md`、`.github/`、`vsm/gates/`、`vsm.toml`、OpenSpec の `spec.md` / `proposal.md` / `design.md` 原本であり、`openspec/changes/**/tasks.md` と `*-result.md` は報告・作業記録として許可する。
- G2 はコード内固定の Docker Compose コマンド `docker compose run --rm --no-deps -u root app sh -c "python -m pip install -q -e '.[dev]' && python -m pytest -q"` を候補 worktree で実行する。
- G3 は `frontend/` に差分がある場合だけ実行し、`package.json` に `lint` script があれば `npm run lint`、続けて `npm run build` を実行する。frontend 差分が無い場合は `skip` で全体判定には影響しない。
- G4 は `python -m vsm --help` と既存の `scripts/smoke_run.py` を実行する。後者は明示的な `FakeLLMProvider` を使い、既存の `FakeAgentRuntime` 経路で mini Run を行う。

Report の各 gate には `pass` / `fail` / `skip`、`duration_ms`、要点、絶対パスの生ログ (`log_path`) を記録する。全 pass または適用外 skip は終了コード0、gate failure は1、Git・Docker・npm 等を実行できない場合は2とする。

Run 文脈がある場合は `vsm.gates.events.record_gate_report_generated` に report を渡すことで、詳細ログを複製せずに `gate_report_generated` を Event_Log に追加できる。イベント payload schema は `vsm/eventlog/schema.py` に追加した。

## CI 変更点

`.github/workflows/test.yml` に次の2ジョブを追加した。

- `frontend-build`: Node 22、`npm ci`、lint、frontend build。
- `gate-runner-g1`: 完全な Git 履歴を checkout し、`python -m vsm.gates.runner --gates g1` を merge-base 相当の ref に対して実行する。

既存の `pytest` ジョブは変更していない。

## テスト・検証

- `tests/unit/test_gate_runner.py`: tmp Git repository による保護 path、secret / `.env`、未追跡、symlink、巨大 diff、`diff --check`、report、終了コード、G3 適用条件、G2/G4 subprocess mock、Event_Log schema を検証する。
- 指定 Compose コマンドによる GateRunner 新規テスト: `13 passed`。
- 指定 Compose コマンドによる全体テスト: `391 passed, 1 skipped`。
- 指定 Compose 環境で `python -m vsm.gates.runner --help` を確認済み。
- 指定 Compose 環境で G4 実行: `pass`（help と Fake mini Run）。

## 残課題

- この作業環境では Windows worktree の `.git` ポインタを WSL Git が解決できず、WSL からこの候補 worktree を Git ref として直接検査できない。GateRunner の G1 は Windows 側の Git または WSL 側で正しく解決できる worktree を前提とする。G2 の実 Docker Compose pytest は全体 pytest と同じ指定コマンドで実施済みだが、GateRunner が自己の G2 を再帰起動する実行確認は未実施である。
- 最終変更後の全体 pytest 再確認は、Compose 内プロセスが10分超無出力で継続し、`docker logs` も空のままツールタイムアウトになった。制約に従いプロセス停止や Docker/WSL の修復は行っていない。直前の成功 run は `391 passed, 1 skipped`、最終変更後の GateRunner テストは `13 passed` である。
- G1 の保護 path を変更する場合は、GateRunner 自体の信頼境界を再設計し、別の信頼済み配布物から実行する必要がある。
