# vsm-poc-platform

Viable System Model (VSM) Proof-of-Concept Platform — a Python 3.11+ / asyncio
single-process implementation of S1〜S5 + S3* with a CLI front-end.

## 概要

`vsm-poc-platform` は Stafford Beer の VSM (Viable System Model) に基づく「AI 自動会社」の
組織アーキテクチャ構想を、動作する PoC ソフトウェアとして実装するための基盤です。

各 System (S1_Worker, S2_Coordinator, S3_Allocator, S3Star_Auditor, S4_Scanner, S5_Policy)
は LLM ベースの AI エージェント (Sub_Agent) として動作し、VSM の標準チャネルを介して
メッセージを交換しながらタスクを処理します。

## アーキテクチャ

- **実装言語**: Python 3.11+
- **並行モデル**: asyncio 単一プロセス
- **LLM 抽象化**: LiteLLM (環境変数 `LITELLM_PROVIDER` または `vsm.toml` で切替)
- **メッセージング**: 自前実装 Message_Bus (外部フレームワーク非依存)
- **永続化**: JSONL ファイル (Source of Truth)、ランタイムキャッシュは Python オブジェクト

## クイックスタート

### 1. インストール

```bash
pip install -e .
```

### 2. LLM プロバイダの設定

```bash
export LITELLM_PROVIDER="openai/gpt-4o-mini"
# または anthropic, bedrock など LiteLLM 対応プロバイダ
export OPENAI_API_KEY="sk-..."
```

または `vsm.toml` に書く:

```toml
[llm]
provider = "openai/gpt-4o-mini"
```

### 3. タスクを投入

```bash
vsm submit "Implement a JSON parser in Python"
```

ファイル付きで投入する場合:

```bash
vsm submit "Refactor this code" --file src/old_module.py --file docs/spec.md
```

### 4. 観測

```bash
# 完了後の状態スナップショット
vsm status <run_id>

# 進行中の Run を tail
vsm tail <run_id>
vsm tail <run_id> --system S4_SCANNER --channel S4-S5

# 完了 Run の全イベントをリプレイ
vsm replay <run_id>
```

## CLI コマンド

| コマンド | 説明 | 主な REQ |
|---|---|---|
| `vsm submit <description> [-f file]...` | タスクを投入し新しい Run を起動 | REQ 4.1〜4.7 |
| `vsm status <run_id>` | Run の Tasks / Systems サマリを stdout 出力 | REQ 11.1 |
| `vsm tail <run_id> [--system S] [--channel C]` | events.jsonl を追従して新着イベントを出力 | REQ 11.2〜11.4 |
| `vsm replay <run_id>` | events.jsonl を append 順で人間可読形式で出力 | REQ 11.5, 11.6 |

## 環境変数

| 環境変数 | 説明 |
|---|---|
| `LITELLM_PROVIDER` | LLM プロバイダのモデル文字列 (例: `openai/gpt-4o-mini`)。`vsm.toml` の `[llm].provider` より優先。 |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc. | 各プロバイダの認証キー (LiteLLM のドキュメントを参照)。 |

## 終了コード

| code | 意味 |
|---|---|
| 0 | 正常終了 |
| 1 | 内部例外 (未分類) |
| 2 | CLI 入力バリデーション違反 (description / file / run_id) |
| 3 | 構造制約違反 (必須 System 不足) |
| 4 | Run ディレクトリ / Event_Log 作成失敗 |
| 5 | スコープ外機能要求 |
| 6 | 代表シナリオの 1800 秒タイムアウト |

## ファイルレイアウト

```
runs/{run_id}/events.jsonl   # 全イベントの Source of Truth (REQ 10.1, 10.3)
runs/{run_id}/RUNNING        # アクティブ Run のロックファイル (vsm replay の警告用)

vsm/
├── cli.py                   # CLI エントリポイント
├── config.py                # vsm.toml + 環境変数のローダ
├── errors.py                # 例外階層
├── ids.py                   # UUIDv4 / run_id バリデータ
├── clock.py                 # UTC clock 抽象 (テスト容易化)
├── roles.py                 # SystemRole enum (S1〜S5, S3*)
├── messaging/               # Message_Bus + ChannelId
├── eventlog/                # JSONL writer / reader / replay
├── llm/                     # LiteLLM ラッパ + FakeLLMProvider
├── systems/                 # S1〜S5 + S3* 実装
└── runtime/                 # Platform オーケストレータ
```

## 開発

### テスト

```bash
pip install -e ".[dev]"  # pytest, hypothesis, pytest-asyncio が入る
pytest                    # 全テスト実行
pytest tests/property     # PBT のみ
pytest tests/integration  # 統合テスト
pytest -m live_llm        # 実 LLM を使うテスト (オプトイン)
```

### ビルドチェック

```bash
python -m py_compile vsm/cli.py vsm/messaging/*.py vsm/eventlog/*.py
```

## MVP Scope Boundaries

REQ 14.9 に従い、本 MVP では以下のスコープ外項目は **意図的に実装していません**:

1. **FSX (Future-State Expansion) の数値最適化・目的関数** (REQ 14.1)
   将来的なエージェントの到達可能状態集合の拡張・維持を目的関数として最大化する機能は本 MVP の対象外です。
2. **公共性の測定および勾配的公共性の評価** (REQ 14.2)
   組織活動の外部影響を測定して FSX 評価範囲を連続的に拡張する仕組みは含みません。
3. **共有剰余の配分ロジック** (REQ 14.3)
   AI が生む剰余の帰属・配分ルールの実装は含みません。
4. **人間の層横断的介入機構** (REQ 14.4)
   テンポラル・インターフェース、サブ VSM デプロイなど、人間が処理速度の異なる本体 VSM に介入するための仕組みは含みません。
5. **VSM の動的な内部分化および外部包摂による再帰的成長** (REQ 14.5)
   サブ VSM の自動増殖や、複数 VSM の上位 VSM への組み込みは含みません。
6. **セミステートフル記憶を S2_Coordinator が集団的に混合する機能** (REQ 14.6)
   各 S1 の記憶を S2 が集約・混合する操作は含みません。
7. **Web UI ダッシュボード** (REQ 14.7)
   HTTP/HTTPS で到達可能な Web UI は提供しません。観測は CLI (`vsm status` / `vsm tail` / `vsm replay`) のみで完結します。

スコープ外機能を CLI に対し要求した場合は終了コード 5 と stderr `requested capability is out of MVP scope: <name>` で拒否されます (REQ 14.8)。

これらの境界は構想ドキュメント (`.kiro/specs/vsm-poc-platform/`) における方向性であり、後続イテレーションでの段階的拡張を想定しています。

## ライセンス

(Proprietary — 本 PoC のライセンスは未確定です)

## 関連ドキュメント

- `.kiro/specs/vsm-poc-platform/requirements.md` — 14 個の Requirement (EARS パターン)
- `.kiro/specs/vsm-poc-platform/design.md` — アーキテクチャ設計、Mermaid シーケンス図、17 個の Correctness Properties
- `.kiro/specs/vsm-poc-platform/tasks.md` — 実装計画 (TDD 順)
