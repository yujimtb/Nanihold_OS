# CLI リファレンス

`vsm` 系 CLI は MVP / テスト実装用の入口であり、タスク投入、状態確認、イベントログ追跡、
リプレイを実行できる。`vsm submit` が使う S4→S5→S3→S1→S3* の固定フローは検証用の暫定実装で
あり、将来のランタイム実装では外す前提。

環境構築は [setup.md](setup.md)、LLM プロバイダ設定も [setup.md](setup.md) を参照。以下は
PowerShell の例。`cmd.exe` では `.\vsm.ps1` の代わりに `vsm.cmd`、Docker Compose では
`docker compose run --rm app python -m vsm ...` を使う。

---

## コマンド一覧

| コマンド | 説明 |
|---|---|
| `.\vsm.ps1 submit "<description>"` | タスクを投入し、新しい Run を起動する。実行中の進捗は stderr に表示され、完了時の stdout は `run_id=...` / `task_id=...` の2行だけになる。 |
| `.\vsm.ps1 submit "<description>" --file path\to\file.txt` | UTF-8 の補足ファイル付きでタスクを投入する。`--file` は複数指定できる。 |
| `.\vsm.ps1 runs` | `runs\` 配下の Run を新しい順に一覧表示する。短縮 run_id、開始時刻、導出状態、イベント数、Run 合計トークン、AgentRuntime 実行時間、タスク概要を確認できる。 |
| `.\vsm.ps1 runs --full-id` | 詳細確認に使うフル run_id 付きで Run 一覧を表示する。 |
| `.\vsm.ps1 status <run_id>` | `events.jsonl` から Task / System の状態と Node 別トークン（input/output/cache read）・AgentRuntime 実行時間を再構成して表示する。 |
| `.\vsm.ps1 tail <run_id>` | Run の `events.jsonl` に追従して新着イベントを JSONL で表示する。 |
| `.\vsm.ps1 tail <run_id> --system <system_id>` | system_id / sender / receiver の一致でイベントを絞り込む。 |
| `.\vsm.ps1 tail <run_id> --channel S4-S5` | channel の一致でイベントを絞り込む。 |
| `.\vsm.ps1 replay <run_id>` | 完了済み Run の全イベントを append 順で表示し、主な payload を短く要約する。 |
| `.\vsm.ps1 replay <run_id> --raw` | 旧来の1イベント1行形式で表示する。 |

`cmd.exe` の例:

```bat
vsm.cmd submit "Write a Python function that reverses a string"
vsm.cmd runs
vsm.cmd status <run_id>
vsm.cmd replay <run_id>
```

`.env` に API キーとモデルが設定済みであれば、追加の環境変数設定なしに `submit` できる。

```powershell
.\vsm.ps1 submit "Write a Python function that reverses a string"
```

Run が完了したら、表示された `run_id` で一覧・状態・イベントを確認できる。

```powershell
.\vsm.ps1 runs
.\vsm.ps1 status <run_id>
.\vsm.ps1 replay <run_id>
```

---

## Run の確認

`submit` が完了すると `run_id` と `task_id` が表示される。

```text
run_id=<run_id>
task_id=<task_id>
```

Run のイベントログは以下に作成される。

```text
runs\<run_id>\events.jsonl
```

最近の Run を確認する例:

```powershell
.\vsm.ps1 runs
.\vsm.ps1 runs --limit 5
.\vsm.ps1 runs --full-id
```

Run の中身を見る例:

```powershell
.\vsm.ps1 status <run_id>
.\vsm.ps1 replay <run_id>
.\vsm.ps1 replay <run_id> --raw
.\vsm.ps1 tail <run_id>
.\vsm.ps1 tail <run_id> --system <system_id>
.\vsm.ps1 tail <run_id> --channel S4-S5
```

---

## 終了コード

| code | 意味 |
|---|---|
| 0 | 正常終了 |
| 1 | 内部例外 |
| 2 | CLI 入力バリデーション違反 |
| 3 | 構造制約違反 |
| 4 | Run ディレクトリ / Event_Log 作成失敗 |
| 5 | MVP スコープ外機能要求 |
| 6 | 代表シナリオの 1800 秒タイムアウト |

---

## 開発コマンド

```powershell
cd D:\userdata\docs\projects\Nanihold_OS
.\.venv-win\Scripts\python.exe -m pip install -e ".[dev]"

.\.venv-win\Scripts\python.exe -m pytest
.\.venv-win\Scripts\python.exe -m pytest tests\unit
.\.venv-win\Scripts\python.exe -m pytest tests\integration
.\.venv-win\Scripts\python.exe -m pytest -m live_llm
```

`dev` extra にはテスト実行に必要な `pytest`、`pytest-asyncio`、`hypothesis` を含めている。
Discord bot は CLI / Web 本体とは別用途なので `bot` extra に分けている。bot も同じ環境で
動かす場合は次を使う。

```powershell
.\.venv-win\Scripts\python.exe -m pip install -e ".[dev,bot]"
```

Docker Compose で実行する場合:

```bash
docker compose run --rm app python -m pytest
docker compose run --rm app python -m pytest tests/unit
```

Python の構文チェック:

```powershell
.\.venv-win\Scripts\python.exe -m py_compile vsm\cli.py vsm\messaging\bus.py vsm\eventlog\reader.py
```

---

## ファイルレイアウト

```text
runs\{run_id}\events.jsonl   # 全イベントの Source of Truth
runs\{run_id}\RUNNING        # アクティブ Run のロックファイル

vsm.cmd                      # cmd.exe 用 VSM ラッパー
vsm.ps1                      # PowerShell 用 VSM ラッパー

vsm\
├── __main__.py              # python -m vsm エントリポイント
├── cli.py                   # CLI エントリポイント
├── config.py                # vsm.toml + .env + 環境変数のローダ
├── errors.py                # 例外階層
├── ids.py                   # UUIDv4 / run_id バリデータ
├── clock.py                 # UTC clock 抽象
├── agents\                  # AgentSpec / AgentInvocation / PromptTemplate
├── architecture\            # EventEnvelope / ProjectionCheckpoint
├── authority\               # ParentAuthority / Lease
├── budget\                  # BudgetContext / BudgetLedger
├── eventlog\                # JSONL writer / reader / replay / schema
├── graph\                   # SQLite adjacency-list graph projection
├── llm\                     # LiteLLM ラッパ + FakeLLMProvider
├── memory\                  # ContextView / TaskSummary / search scope
├── messaging\               # Message_Bus + ChannelId
├── nodes\                   # Node / NodeRunState / lifecycle
├── roles\                   # SystemRole / RoleSpec
├── runtime\                 # Platform / topology / Execution / QuotaMonitor
├── systems\                 # S1〜S5 + S3* 実装
├── telemetry\               # Event_Log と OpenTelemetry の相関値
└── tools\                   # ToolEffect / ToolInvocation / facade
```
