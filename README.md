# Nanihold OS

Viable System Model (VSM) runtime platform.

Python 3.11+ / asyncio による単一プロセス実装で、S1〜S5 + S3* の各 System
が LLM ベースの Sub_Agent として動作します。CLI からタスク投入、状態確認、
イベントログ追跡、リプレイを実行できます。

この README は Windows 版の実行手順を基準にしています。

## WSL / コンテナ開発

Windows でコンテナ開発する場合は、WSL2 + Ubuntu 上にリポジトリを置き、
Docker Desktop の WSL integration を有効にして使います。Windows 側の
`D:\...` 配下を直接 bind mount するより、WSL の Linux ファイルシステム
(`~/projects/Nanihold_OS` など) に置く方がファイル I/O と権限差分が安定します。

```powershell
wsl --install -d Ubuntu
wsl -l -v
```

Ubuntu 側でリポジトリを開きます。

```bash
mkdir -p ~/projects
cd ~/projects
git clone <repo-url> Nanihold_OS
cd Nanihold_OS
code .
```

VS Code Dev Containers を使う場合は、`Reopen in Container` を実行します。
エディタ非依存で使う場合は Docker Compose から同じ環境を起動できます。

```bash
docker compose build
docker compose run --rm app python -m pytest
docker compose run --rm app python scripts/smoke_run.py
docker compose run --rm app python -m vsm --help
```

実 LLM を使う場合は、各自の WSL 側リポジトリ直下に `.env` を作成します。
`.env` は Git 管理しません。キー名の雛形は `.env.example` を参照してください。

## 概要

`Nanihold OS` は Stafford Beer の VSM (Viable System Model) に基づく
「AI 自動会社」の組織アーキテクチャ構想を、動作する PoC ソフトウェアとして
確認するための基盤です。

各 System (`S1_WORKER`, `S2_COORDINATOR`, `S3_ALLOCATOR`,
`S3STAR_AUDITOR`, `S4_SCANNER`, `S5_POLICY`) は VSM の標準チャネルを介して
メッセージを交換し、Run ごとの `events.jsonl` に全イベントを永続化します。
現在は `refactor_20260608.md` に沿って、従来の System 実装の上に
Architecture / Role / Agent / Tool / Node / Authority / Projection の層を
追加しています。Run は Platform の起動停止ではなく、外部入力や監査要求を
観測・会計する単位として扱い、永続的な責任と履歴は Node が保持します。

## Windows クイックスタート

以下は PowerShell での手順です。作業ディレクトリはこのリポジトリのルートです。

```powershell
cd D:\userdata\docs\projects\Nanihold_OS
```

### 1. Python 仮想環境を作成

Python 3.11 以上が必要です。

```powershell
py -3.11 -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install --upgrade pip
.\.venv-win\Scripts\python.exe -m pip install -e .
```

開発用のテスト依存も入れる場合:

```powershell
.\.venv-win\Scripts\python.exe -m pip install -e ".[dev]"
```

### 2. CLI の起動確認

PowerShell では同梱のラッパーを使うのが簡単です。

```powershell
.\vsm.ps1 --help
```

`Activate.ps1` や `vsm.ps1` が実行ポリシーで止まる場合は、その PowerShell
セッションだけ一時的に許可してから再実行します。

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\vsm.ps1 --help
```

`cmd.exe` から使う場合:

```bat
cd /d D:\userdata\docs\projects\Nanihold_OS
vsm.cmd --help
```

仮想環境を有効化して使う場合:

```powershell
.\.venv-win\Scripts\Activate.ps1
vsm.exe --help
```

仮想環境を有効化せず、Python モジュールとして直接実行する場合:

```powershell
.\.venv-win\Scripts\python.exe -m vsm --help
```

## LLM なしで動作確認

API キーなしで、VSM の構造、メッセージング、Event_Log、リプレイ可能な
イベント永続化を確認できます。`FakeLLMProvider` が固定応答を返します。

```powershell
cd D:\userdata\docs\projects\Nanihold_OS
.\.venv-win\Scripts\python.exe scripts\smoke_run.py
```

これは LLM の推論品質を見るものではなく、S4 → S5 → S3 → S1 → S3* の
イベント伝播とランタイム構造を確認するための smoke test です。

## 実 LLM で VSM を使う

`vsm submit` は LiteLLM 経由でモデルを呼び出します。プロバイダは
`LITELLM_PROVIDER` 環境変数、または `vsm.toml` / `.env` で設定します。

### 既に `.env` に API キーとモデルを書いてある場合

リポジトリ直下の `.env` に `LITELLM_PROVIDER` とプロバイダ別 API キー
(`OPENROUTER_API_KEY`, `OPENAI_API_KEY` など) が入っている場合は、
PowerShell で `$env:...` を設定する必要はありません。

```powershell
cd D:\userdata\docs\projects\Nanihold_OS
.\vsm.ps1 submit "Write a Python function that reverses a string"
```

`cmd.exe` でも同じ `.env` が読み込まれます。

```bat
cd /d D:\userdata\docs\projects\Nanihold_OS
vsm.cmd submit "Write a Python function that reverses a string"
```

Run が完了したら、表示された `run_id` で状態確認できます。

```powershell
.\vsm.ps1 status <run_id>
.\vsm.ps1 replay <run_id>
```

`.env` は `vsm submit` の実行時に読み込まれます。シェル側で
`LITELLM_PROVIDER` を設定している場合は、シェルの値が `.env` より優先されます。

### OpenAI を使う例

PowerShell:

```powershell
cd D:\userdata\docs\projects\Nanihold_OS
$env:LITELLM_PROVIDER = "openai/gpt-4o-mini"
$env:OPENAI_API_KEY = "sk-..."

.\vsm.ps1 submit "Write a Python function that reverses a string"
```

`cmd.exe`:

```bat
cd /d D:\userdata\docs\projects\Nanihold_OS
set LITELLM_PROVIDER=openai/gpt-4o-mini
set OPENAI_API_KEY=sk-...

vsm.cmd submit "Write a Python function that reverses a string"
```

### OpenRouter を使う例

`.env` に設定すると、毎回 PowerShell で `$env:...` を設定せずに使えます。
`.env` は Git 管理しないローカル認証情報として扱ってください。

```dotenv
LITELLM_PROVIDER=openrouter/openai/gpt-oss-20b:free
OPENROUTER_API_KEY=sk-or-v1-...
```

`.env` 設定済みなら、実行コマンドはこれだけです。

```powershell
.\vsm.ps1 submit "Write a Python function that reverses a string"
```

OpenRouter のモデル ID は LiteLLM 向けに先頭へ `openrouter/` を付けます。
たとえば OpenRouter 側のモデル ID が `openai/gpt-oss-20b:free` の場合、
`LITELLM_PROVIDER` は `openrouter/openai/gpt-oss-20b:free` です。

無料モデルは availability や rate limit で失敗することがあります。その場合は
OpenRouter の Models 画面で別の `:free` モデル ID を選び、同じく
`openrouter/` を付けて指定します。

### Bedrock を使う例

AWS 認証と Bedrock のモデルアクセス権限がある場合:

```powershell
$env:LITELLM_PROVIDER = "bedrock/anthropic.claude-3-5-haiku-20241022-v1:0"
$env:AWS_REGION = "us-west-2"

.\vsm.ps1 submit "Summarize the current VSM architecture"
```

## VSM CLI コマンド集

| コマンド | 説明 |
|---|---|
| `.\vsm.ps1 submit "<description>"` | タスクを投入し、新しい Run を起動します。 |
| `.\vsm.ps1 submit "<description>" --file path\to\file.txt` | UTF-8 の補足ファイル付きでタスクを投入します。`--file` は複数指定できます。 |
| `.\vsm.ps1 status <run_id>` | `events.jsonl` から Task / System の状態サマリを再構成して表示します。 |
| `.\vsm.ps1 tail <run_id>` | Run の `events.jsonl` に追従して新着イベントを JSONL で表示します。 |
| `.\vsm.ps1 tail <run_id> --system <system_id>` | system_id / sender / receiver の一致でイベントを絞り込みます。 |
| `.\vsm.ps1 tail <run_id> --channel S4-S5` | channel の一致でイベントを絞り込みます。 |
| `.\vsm.ps1 replay <run_id>` | 完了済み Run の全イベントを append 順で人間可読形式に表示します。 |

`cmd.exe` では `.\vsm.ps1` の代わりに `vsm.cmd` を使います。

```bat
vsm.cmd submit "Write a Python function that reverses a string"
vsm.cmd status <run_id>
vsm.cmd replay <run_id>
```

## Run の確認

`submit` が完了すると `run_id` と `task_id` が表示されます。

```text
run_id=<run_id>
task_id=<task_id>
```

Run のイベントログは以下に作成されます。

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

## 設定ファイル

環境変数の代わりに `vsm.toml` にモデルを設定できます。

```toml
[llm]
provider = "openai/gpt-4o-mini"
```

`LITELLM_PROVIDER` 環境変数が設定されている場合は、`vsm.toml` の
`[llm].provider` より優先されます。

## 環境変数

| 環境変数 | 説明 |
|---|---|
| `LITELLM_PROVIDER` | LiteLLM のモデル文字列。例: `openai/gpt-4o-mini`, `openrouter/openai/gpt-oss-20b:free` |
| `OPENAI_API_KEY` | OpenAI を使う場合の API キー。 |
| `OPENROUTER_API_KEY` | OpenRouter を使う場合の API キー。 |
| `ANTHROPIC_API_KEY` | Anthropic を使う場合の API キー。 |
| `AWS_REGION` | Bedrock を使う場合の AWS リージョン。 |

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

## 開発コマンド

```powershell
cd D:\userdata\docs\projects\Nanihold_OS
.\.venv-win\Scripts\python.exe -m pip install -e ".[dev]"

.\.venv-win\Scripts\python.exe -m pytest
.\.venv-win\Scripts\python.exe -m pytest tests\unit
.\.venv-win\Scripts\python.exe -m pytest tests\integration
.\.venv-win\Scripts\python.exe -m pytest -m live_llm
```

Python の構文チェック:

```powershell
.\.venv-win\Scripts\python.exe -m py_compile vsm\cli.py vsm\messaging\bus.py vsm\eventlog\reader.py
```

## Discord Codex Bot

WSL 側のリポジトリ (`/home/user/projects/Nanihold_OS`) で Codex CLI を実行し、
Discord スレッドから自然言語でコーディングを依頼できます。

Ubuntu 側で Codex CLI と bot 依存を用意します。

```bash
sudo apt-get install -y nodejs npm
sudo npm install -g @openai/codex
cd /home/user/projects/Nanihold_OS
. .venv/bin/activate
python -m pip install -e .
```

WSL 側で Codex 認証も済ませます。

```bash
codex login
codex doctor
```

`.env` に Discord bot 用の値を追加します。

```dotenv
DISCORD_BOT_TOKEN=...
DISCORD_ALLOWED_USER_IDS=123456789012345678,234567890123456789
DISCORD_ALLOWED_CHANNEL_IDS=345678901234567890
CODEX_WORKDIR=/home/user/projects/Nanihold_OS
CODEX_BIN=codex
CODEX_TIMEOUT_SECONDS=1800
CODEX_LOG_DIR=logs/discord-codex
```

Discord Developer Portal では bot の `Message Content Intent` を有効にしてください。
通常チャンネルでは `!codex <依頼内容>` または bot へのメンションで開始します。
bot が作成した `codex-...` スレッド内では、その後の自然文メッセージを Codex に渡します。

設定確認と手動起動:

```bash
python bot/discord_codex_bot.py --check
python bot/discord_codex_bot.py
```

常駐化:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/discord-codex-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now discord-codex-bot
systemctl --user status discord-codex-bot
journalctl --user -u discord-codex-bot -f
```

初期設定では `git push`、`git reset --hard`、`.env` の内容表示などは bot 側で
止めます。Codex 実行ログは `logs/discord-codex/` に保存されます。

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

## Refactor 20260608 実装状況

`refactor_20260608.md` の基礎方針に対して、現在の実装は以下の状態です。

| 項目 | 実装 |
|---|---|
| EventEnvelope v1 | `vsm.eventlog.schema.Event` と `vsm.architecture.events.EventEnvelope`。`event_id`, `stream_id`, `stream_version`, `schema_version`, `correlation_id`, `causation_id` を持ちます。 |
| Projection checkpoint | `vsm.architecture.projections.ProjectionCheckpoint`。処理済み `event_id` を保持して同一イベント再適用を防ぎます。 |
| Node / NodeRunState | `vsm.nodes.model.Node`, `NodeRunState`。`NodeSource` により `terminable=False` は config 由来の Node のみに制限します。 |
| static / live topology | `vsm.runtime.topology.StaticTopologyEntry`, `LiveTopology`。Event_Log 由来の `node_created`, `node_differentiated`, lifecycle event を反映します。 |
| ParentAuthority / Lease | `vsm.authority.ParentAuthority`, `Lease`。分化上限、Tool effect 制限、外部資源 lease を表します。 |
| ToolEffect / idempotency | `vsm.tools.ToolEffect`, `ToolInvocation`。`EXTERNAL_WRITE` と `CONTROL` は `idempotency_key` 必須です。 |
| Tool facade | `CoordinationFacade`, `DifferentiationFacade`, `EscalationFacade`。S2 調停、分化、エスカレーション要求を冪等な CONTROL Tool として扱います。 |
| Role / Agent / Execution | `RoleSpec`, `AgentSpec`, `PromptTemplate`, `Execution`。Spec versioning と Agent / Tool 実行単位を明示します。 |
| Memory / Graph / Telemetry | `ContextView`, `TaskSummary`, `GraphProjection`, `TelemetryCorrelation` を軽量モデルとして実装しています。 |

## Current Scope and Roadmap

Nanihold OS は MVP 境界を越え、VSM ランタイムとしての実装範囲を拡張中です。
S1_Worker は LLM 応答を `s1_completion` の `result` に記録し、S1〜S5 + S3*
の各 System、Event_Log、Node / ParentAuthority、Tool facade、Projection、
Role / Agent / Execution の基礎モデルを組み合わせて実行されます。

現在の主要な実装状況は以下の通りです。

- REQ 14.1: FSX (Future-State Expansion) の数値最適化・目的関数評価は未実装です。
- REQ 14.2: 公共性測定および勾配的公共性評価は未実装です。
- REQ 14.3: 共有剰余の配分ロジックは未実装です。
- REQ 14.4: サブ VSM デプロイは機能として実装済みです。人間の層横断介入と
  テンポラル・インターフェースは今後の拡張対象です。
- REQ 14.5: 動的な内部分化・外部包摂による再帰的成長は、`differentiate` /
  `request_escalation` などの Tool facade と Node / ParentAuthority の基礎モデルを
  実装済みです。自律運用するランタイムポリシーは段階的に有効化します。
- REQ 14.6: S2_Coordinator によるセミステートフル記憶の集団的混合は未実装です。
- REQ 14.7: HTTP / HTTPS で到達可能な Web UI ダッシュボードは未実装ですが、実装を予定しています。

コード実行、ファイル編集、外部プロセス実行は短期ロードマップの対象です。
これらは ToolEffect / ToolInvocation の effect 境界、idempotency key、
ParentAuthority / Lease による権限管理と組み合わせて、安全な実行単位として
導入する方針です。

永続的な会社運用と Run 間の長期記憶は、Node と Event_Log を中心に扱う方向で
設計を進めています。現時点では FSX、公共性評価、共有剰余配分などの評価・分配
アルゴリズムが主な未実装領域です。

## 関連ドキュメント

- `refactor_20260608.md`
- `.kiro\specs\nanihold-os\requirements.md`
- `.kiro\specs\nanihold-os\design.md`
- `.kiro\specs\nanihold-os\tasks.md`

## ライセンス

Proprietary. 本 PoC のライセンスは未確定です。
