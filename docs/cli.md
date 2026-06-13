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
| `.\vsm.ps1 submit "<description>"` | タスクを投入し、新しい Run を起動する。 |
| `.\vsm.ps1 submit "<description>" --file path\to\file.txt` | UTF-8 の補足ファイル付きでタスクを投入する。`--file` は複数指定できる。 |
| `.\vsm.ps1 status <run_id>` | `events.jsonl` から Task / System の状態サマリを再構成して表示する。 |
| `.\vsm.ps1 tail <run_id>` | Run の `events.jsonl` に追従して新着イベントを JSONL で表示する。 |
| `.\vsm.ps1 tail <run_id> --system <system_id>` | system_id / sender / receiver の一致でイベントを絞り込む。 |
| `.\vsm.ps1 tail <run_id> --channel S4-S5` | channel の一致でイベントを絞り込む。 |
| `.\vsm.ps1 replay <run_id>` | 完了済み Run の全イベントを append 順で人間可読形式に表示する。 |

`cmd.exe` の例:

```bat
vsm.cmd submit "Write a Python function that reverses a string"
vsm.cmd status <run_id>
vsm.cmd replay <run_id>
```

`.env` に API キーとモデルが設定済みであれば、追加の環境変数設定なしに `submit` できる。

```powershell
.\vsm.ps1 submit "Write a Python function that reverses a string"
```

Run が完了したら、表示された `run_id` で状態確認できる。

```powershell
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
Get-ChildItem .\runs | Sort-Object LastWriteTime -Descending | Select-Object -First 5
```

Run の中身を見る例:

```powershell
.\vsm.ps1 status <run_id>
.\vsm.ps1 replay <run_id>
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
├── runtime\                 # Platform / topology / Execution
├── systems\                 # S1〜S5 + S3* 実装
├── telemetry\               # Event_Log と OpenTelemetry の相関値
└── tools\                   # ToolEffect / ToolInvocation / facade
```
