# セットアップ

Nanihold OS の開発・実行環境の構築手順。標準は **WSL2 + Docker Compose** だが、Windows
ネイティブの仮想環境でも動かせる。

- WSL / Docker Compose: 本書「WSL / コンテナ開発」
- Windows ネイティブ: 本書「Windows ネイティブ環境」
- Codex アプリからの開発: 本書「Codex アプリからの開発」
- LLM プロバイダ設定: 本書「LLM プロバイダの設定」

CLI の使い方は [cli.md](cli.md)、Web UI は [web-ui.md](web-ui.md) を参照。

---

## WSL / コンテナ開発(標準)

Windows でコンテナ開発する場合は、WSL2 + Ubuntu 上にリポジトリを置き、Docker Desktop の
WSL integration を有効にして使う。Windows 側の `D:\...` 配下を直接 bind mount するより、
WSL の Linux ファイルシステム(`~/projects/Nanihold_OS` など)に置く方がファイル I/O と権限
差分が安定する。

```powershell
wsl --install -d Ubuntu
wsl -l -v
```

Ubuntu 側でリポジトリを開く。

```bash
mkdir -p ~/projects
cd ~/projects
git clone <repo-url> Nanihold_OS
cd Nanihold_OS
code .
```

VS Code Dev Containers を使う場合は `Reopen in Container` を実行する。エディタ非依存で使う
場合は Docker Compose から同じ環境を起動できる。

```bash
docker compose build
docker compose run --rm app python -m pytest
docker compose run --rm app python scripts/smoke_run.py
docker compose run --rm app python -m vsm --help
```

pytest の一時ファイルはリポジトリ直下の `.pytest-tmp/` に作成される。このディレクトリは
テスト起動時に自動作成され、Git 管理外として扱う。

実 LLM を使う場合は、WSL 側リポジトリ直下に `.env` を作成する(後述「LLM プロバイダの設定」)。
`.env` は Git 管理しない。

---

## Windows ネイティブ環境

Docker を使わず Windows の Python 仮想環境で動かす場合の手順。作業ディレクトリはリポジトリの
ルート。

### 1. Python 仮想環境を作成

Python 3.11 以上が必要。

```powershell
cd D:\userdata\docs\projects\Nanihold_OS
py -3.11 -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install --upgrade pip
.\.venv-win\Scripts\python.exe -m pip install -e .
```

開発用のテスト依存も入れる場合:

```powershell
.\.venv-win\Scripts\python.exe -m pip install -e ".[dev]"
```

### 2. CLI の起動確認

PowerShell では同梱のラッパーを使うのが簡単。

```powershell
.\vsm.ps1 --help
```

`Activate.ps1` や `vsm.ps1` が実行ポリシーで止まる場合は、その PowerShell セッションだけ一時的に
許可してから再実行する。

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

### 3. LLM なしで動作確認

API キーなしで、VSM の構造、メッセージング、Event_Log、リプレイ可能なイベント永続化を確認
できる。`FakeLLMProvider` が固定応答を返す。

```powershell
cd D:\userdata\docs\projects\Nanihold_OS
.\.venv-win\Scripts\python.exe scripts\smoke_run.py
```

これは LLM の推論品質を見るものではなく、現行 MVP の暫定的な固定フロー
(S4 → S5 → S3 → S1 → S3*)に沿ったイベント伝播とランタイム構造を確認するための smoke test。
この固定フローは検証用であり、本来のランタイムは固定経路を持たない([architecture.md](architecture.md))。
イベントログは通常の Run と同じく `runs\<run_id>\events.jsonl` に作成される。出力された
`run_id` はそのまま `runs` / `status` / `replay` で確認できる。

```powershell
.\vsm.ps1 runs
.\vsm.ps1 status <smoke run_id>
.\vsm.ps1 replay <smoke run_id>
```

---

## Codex アプリからの開発

Codex アプリのシェルが Windows PowerShell の場合でも、Windows 側の Python や UNC パス上の
Docker 実行は使わない。リポジトリ直下のラッパーから、WSL 内の
`/home/user/projects/Nanihold_OS` と Docker Compose `app` サービスへ転送する。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 doctor
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 up
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 install
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 test
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 vsm --help
```

任意の Compose コマンドやコンテナ内コマンドも同じ入口から実行できる。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 compose ps
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 exec python scripts/smoke_run.py
```

手元の PowerShell から短く実行したい場合は、同梱の `.\codex-dev.cmd` も同じサブコマンドを
受け付ける。Codex アプリでは UNC パス警告を避けるため、上記の PowerShell 直呼びを優先する。

別の WSL 配布名やチェックアウト先を使う場合は、PowerShell 側で次の環境変数を指定する。

```powershell
$env:NANIHOLD_WSL_DISTRO = "Ubuntu"
$env:NANIHOLD_WSL_PROJECT_DIR = "/home/user/projects/Nanihold_OS"
```

> エージェント(Codex / Claude Code 等)向けの開発規約は、リポジトリ直下の `AGENTS.md` を参照。

---

## AgentRuntime の設定

`vsm submit` はロールごとに Claude Code / Codex / LiteLLM / fake のいずれかを使う。
既定は S1 が Codex、S3 Allocator は決定論処理、その他の AI ロールは Claude Code である。
Claude Code と Codex は、それぞれのサブスクリプション CLI で事前に認証しておく。

設定は `vsm.toml` の `[agents]` に集約する。CLI 実行ファイルだけは一時上書きとして
`CLAUDE_BIN` / `CODEX_BIN` を使用できる。

```toml
[agents]
default_backend = "claude-code"

[agents.backends.claude-code]
bin = "claude"
model = ""
timeout_seconds = 1800

[agents.backends.codex]
bin = "codex"
model = "gpt-5.6-sol"
reasoning_effort = "high"
timeout_seconds = 1800

[agents.roles]
S5_POLICY = "claude-code"
S4_SCANNER = "claude-code"
S3_ALLOCATOR = ""
S2_COORDINATOR = "claude-code"
S3STAR_AUDITOR = "claude-code"
S1_WORKER = "codex"

[session]
resume_within_run = true

[budget]
run_tokens = 2000000
run_wall_clock_seconds = 7200

[budget.roles]
S1_WORKER = { tokens = 500000, wall_clock_seconds = 1800 }

[quota]
suspend_on_exhausted = true
fallback_resume_minutes = 60
weekly_fallback_resume_minutes = 360
```

`resume_within_run = true` の場合、同一 Run・同一 Node・同一 backend の2回目以降の
呼び出しは CLI セッションを再開し、再開時には重複する context view を送らない。セッションが
消滅していた場合だけ、新規セッションと完全な context view で1回再試行する。セッション参照は
Run 終了時に破棄され、Run 間では引き継がれない。

空文字を割り当てたロールには AgentRuntime を注入しない。未認識のバックエンドや不正な
設定値は起動時エラーとなり、別バックエンドへの暗黙の切り替えは行わない。

`[budget]` は Run 全体のトークン合計（input + output + cache read）と AgentRuntime
呼び出し時間を制限する。`[budget.roles]` に指定したロールは個別 envelope を使い、未指定
ロールは Run envelope を使う。既消費量が上限以上の呼び出しは実行前に拒否され、
`budget_exceeded` と `escalation_requested` が記録される。

quota 枯渇を返したバックエンドの Node は `SUSPENDED` になり、`quota_reset_at`、または
時刻不明時の `fallback_resume_minutes` に自動復帰する。休眠中および枯渇検知時に処理中だった
Message は Node 別キューに保持され、復帰後に再投入される。

### LiteLLM を明示的に使う場合

API 経由のモデルが必要なロールだけ `[agents.roles]` で `litellm` を指定し、
`LITELLM_PROVIDER` 環境変数または `[llm].provider` を設定する。`.env` は Git 管理しない
ローカル認証情報として扱う。キー名の雛形は `.env.example` を参照。
以下の API 例は、対象ロールを `[agents.roles]` で `litellm` に変更済みであることを前提とする。

#### `.env` で設定する

リポジトリ直下の `.env` に `LITELLM_PROVIDER` とプロバイダ別 API キーを書いておくと、毎回
`$env:...` を設定せずに使える。

```dotenv
LITELLM_PROVIDER=openrouter/openai/gpt-oss-20b:free
OPENROUTER_API_KEY=sk-or-v1-...
```

`.env` はシェル(PowerShell / cmd.exe いずれも)から `vsm submit` を実行する際に読み込まれる。
シェル側で `LITELLM_PROVIDER` を設定している場合は、シェルの値が `.env` より優先される。

#### OpenAI を使う例

```powershell
$env:LITELLM_PROVIDER = "openai/gpt-4o-mini"
$env:OPENAI_API_KEY = "sk-..."
.\vsm.ps1 submit "Write a Python function that reverses a string"
```

#### OpenRouter を使う例

OpenRouter のモデル ID は LiteLLM 向けに先頭へ `openrouter/` を付ける。たとえば OpenRouter 側の
モデル ID が `openai/gpt-oss-20b:free` の場合、`LITELLM_PROVIDER` は
`openrouter/openai/gpt-oss-20b:free` となる。

無料モデルは availability や rate limit で失敗することがある。その場合は OpenRouter の Models
画面で別の `:free` モデル ID を選び、同じく `openrouter/` を付けて指定する。

#### Bedrock を使う例

AWS 認証と Bedrock のモデルアクセス権限がある場合:

```powershell
$env:LITELLM_PROVIDER = "bedrock/anthropic.claude-3-5-haiku-20241022-v1:0"
$env:AWS_REGION = "us-west-2"
.\vsm.ps1 submit "Summarize the current VSM architecture"
```

#### `vsm.toml` で LiteLLM を設定する

環境変数の代わりに `vsm.toml` にモデルを設定できる。

```toml
[llm]
provider = "openai/gpt-4o-mini"

[agents.roles]
S5_POLICY = "litellm"
```

`LITELLM_PROVIDER` 環境変数が設定されている場合は、`vsm.toml` の `[llm].provider` より優先される。

### 環境変数一覧

| 環境変数 | 説明 |
|---|---|
| `CLAUDE_BIN` | Claude Code CLI の実行ファイルを一時上書きする。 |
| `CODEX_BIN` | Codex CLI の実行ファイルを一時上書きする。 |
| `LITELLM_PROVIDER` | LiteLLM のモデル文字列。例: `openai/gpt-4o-mini`, `openrouter/openai/gpt-oss-20b:free` |
| `OPENAI_API_KEY` | OpenAI を使う場合の API キー。 |
| `OPENROUTER_API_KEY` | OpenRouter を使う場合の API キー。 |
| `ANTHROPIC_API_KEY` | Anthropic を使う場合の API キー。 |
| `AWS_REGION` | Bedrock を使う場合の AWS リージョン。 |
