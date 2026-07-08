# vsm-poc-platform

Viable System Model (VSM) Proof-of-Concept Platform.

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

`vsm-poc-platform` は Stafford Beer の VSM (Viable System Model) に基づく
「AI 自動会社」の組織アーキテクチャ構想を、動作する PoC ソフトウェアとして
確認するための基盤です。

各 System (`S1_WORKER`, `S2_COORDINATOR`, `S3_ALLOCATOR`,
`S3STAR_AUDITOR`, `S4_SCANNER`, `S5_POLICY`) は VSM の標準チャネルを介して
メッセージを交換し、Run ごとの `events.jsonl` に全イベントを永続化します。

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
イベント伝播とランタイム構造を確認するための smoke test です。イベントログは
通常の Run と同じく `runs\<run_id>\events.jsonl` に作成されます。
出力された `run_id` はそのまま `runs` / `status` / `replay` で確認できます。

```powershell
.\vsm.ps1 runs
.\vsm.ps1 status <smoke run_id>
.\vsm.ps1 replay <smoke run_id>
```

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
| `.\vsm.ps1 submit "<description>"` | タスクを投入し、新しい Run を起動します。実行中の進捗は stderr に表示され、完了時の stdout は `run_id=...` / `task_id=...` の2行だけです。 |
| `.\vsm.ps1 submit "<description>" --file path\to\file.txt` | UTF-8 の補足ファイル付きでタスクを投入します。`--file` は複数指定できます。 |
| `.\vsm.ps1 runs` | `runs\` 配下の Run を新しい順に一覧表示します。短縮 run_id、開始時刻、導出状態、イベント数、タスク概要を確認できます。 |
| `.\vsm.ps1 runs --full-id` | 詳細確認に使うフル run_id 付きで Run 一覧を表示します。 |
| `.\vsm.ps1 status <run_id>` | `events.jsonl` から Task / System の状態サマリを再構成して表示します。`s1_completion` などの既存イベントから完了状態を導出します。 |
| `.\vsm.ps1 tail <run_id>` | Run の `events.jsonl` に追従して新着イベントを JSONL で表示します。 |
| `.\vsm.ps1 tail <run_id> --system <system_id>` | system_id / sender / receiver の一致でイベントを絞り込みます。 |
| `.\vsm.ps1 tail <run_id> --channel S4-S5` | channel の一致でイベントを絞り込みます。 |
| `.\vsm.ps1 replay <run_id>` | 完了済み Run の全イベントを append 順で表示し、主な payload を短く要約します。 |
| `.\vsm.ps1 replay <run_id> --raw` | 旧来の1イベント1行形式で表示します。 |

`cmd.exe` では `.\vsm.ps1` の代わりに `vsm.cmd` を使います。

```bat
vsm.cmd submit "Write a Python function that reverses a string"
vsm.cmd runs
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
.\vsm.ps1 runs
.\vsm.ps1 runs --limit 5
.\vsm.ps1 runs --full-id
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

`dev` extra にはテスト実行に必要な `pytest`、`pytest-asyncio`、`hypothesis`
を含めています。Discord bot は VSM CLI 本体とは別用途なので `bot` extra に
分けています。bot も同じ環境で動かす場合は次を使います。

```powershell
.\.venv-win\Scripts\python.exe -m pip install -e ".[dev,bot]"
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
python -m pip install -e ".[bot]"
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
├── roles.py                 # SystemRole enum
├── messaging\               # Message_Bus + ChannelId
├── eventlog\                # JSONL writer / reader / replay
├── llm\                     # LiteLLM ラッパ + FakeLLMProvider
├── systems\                 # S1〜S5 + S3* 実装
└── runtime\                 # Platform オーケストレータ
```

## MVP Scope Boundaries

この MVP は VSM アーキテクチャの動作確認用です。S1_Worker は LLM 応答を
`s1_completion` の `result` に記録しますが、実際にコードを書いたり、
ファイルを編集したり、外部プロセスを実行したりはしません。

- REQ 14.1: FSX (Future-State Expansion) の数値最適化・目的関数評価は実装しません。
- REQ 14.2: 公共性測定および勾配的公共性評価は実装しません。
- REQ 14.3: 共有剰余の配分ロジックは実装しません。
- REQ 14.4: 人間の層横断介入、テンポラル・インターフェース、サブ VSM デプロイは実装しません。
- REQ 14.5: 動的な内部分化・外部包摂による再帰的成長は実装しません。
- REQ 14.6: S2_Coordinator によるセミステートフル記憶の集団的混合は実装しません。
- REQ 14.7: HTTP / HTTPS で到達可能な Web UI ダッシュボードは実装しません。

永続的な会社運用、Run 間の長期記憶、コード実行サンドボックスも本 PoC の
スコープ外です。

## 関連ドキュメント

- `.kiro\specs\vsm-poc-platform\requirements.md`
- `.kiro\specs\vsm-poc-platform\design.md`
- `.kiro\specs\vsm-poc-platform\tasks.md`

## ライセンス

Proprietary. 本 PoC のライセンスは未確定です。
