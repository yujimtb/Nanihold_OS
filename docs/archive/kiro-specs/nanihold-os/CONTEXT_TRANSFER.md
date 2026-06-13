# Context Transfer: Nanihold OS

**最終更新**: 2026-05-24
**ステータス**: MVP 実装完了、全 285 テスト green、E2E 動作確認済み

このドキュメントは、新しい会話セッションや別の協働者が本 PoC を引き継ぐために必要な全コンテキストを 1 ファイルにまとめたものです。

---

## 1. プロジェクト概要

### 1.1 何を作っているか

`Nanihold OS` は、Stafford Beer の **VSM (Viable System Model)** に基づく「AI 自動会社」の組織アーキテクチャ構想を、**動作する Python ソフトウェア** として実装した PoC。

各 System (S1〜S5, S3*) は LLM ベースの AI エージェント (Sub_Agent) として動作し、自前実装の Message_Bus を介してメッセージを交換しながらタスクを処理する。

### 1.2 構想の出典

`.kiro/specs/nanihold-os/` に隣接する形で、ユーザー (三戸部友治) が提供した構想ドキュメント "AI自動会社の組織アーキテクチャ構想：VSM・FSX・労働の自律性の統合" がある。本 PoC は **構想ドキュメントの FSX に関わる数値最適化部分を意図的に除外**して MVP 化した版。

### 1.3 MVP のスコープ

**含めたもの**:
- VSM の S1〜S5 + S3* 監査機構をソフトウェア構造として表現
- LLM ベース Sub_Agent 実行 (LiteLLM 経由、OpenAI/Anthropic/Bedrock 切替可能)
- VSM 標準 7 チャネル (S1↔S2、S1↔S3、S3↔S4、S3↔S5、S4↔S5、S3*→S1、S3*→S5) の自前 Message_Bus
- JSONL を Source of Truth とする永続化 (replay でランタイム状態を完全再構成)
- CLI 4 サブコマンド: `submit` / `status` / `tail` / `replay`
- 代表シナリオの End-to-End: 環境 → S4 → S5 → (S3 + S4 並行) → S1 動的生成 ↔ S2 → S3* 監査

**含めないもの (REQ 14 で SHALL NOT)**:
- FSX (Future-State Expansion) 数値最適化
- 公共性測定・勾配的公共性
- 共有剰余配分
- 人間の層横断介入機構 (テンポラル・インターフェース、サブVSM デプロイ)
- VSM の動的内部分化・外部包摂による再帰的成長
- セミステートフル記憶を S2 が集団的に混合する機能
- Web UI ダッシュボード

---

## 2. リポジトリ構造

### 2.1 ファイルレイアウト

```
/Users/rkhashim/Kiro/Others/
├── .kiro/specs/nanihold-os/
│   ├── requirements.md       # 14 個の Requirement (EARS パターン)
│   ├── design.md             # アーキテクチャ + 17 Properties + Mermaid 図
│   ├── tasks.md              # 84 タスクの実装計画 (全 completed)
│   ├── .config.kiro          # spec メタデータ (specType=feature, workflowType=requirements-first)
│   └── CONTEXT_TRANSFER.md   # このファイル
├── vsm/                      # 本体パッケージ
│   ├── cli.py                # Typer ベースの CLI (submit/status/tail/replay + scope guard)
│   ├── config.py             # vsm.toml + 環境変数のローダ (LLMConfig, RunConfig)
│   ├── errors.py             # 15 例外クラスの階層
│   ├── ids.py                # UUIDv4 生成 + run_id バリデータ
│   ├── clock.py              # SystemClock + FakeClock (テスト容易化)
│   ├── roles.py              # SystemRole enum + MANDATORY_ROLES
│   ├── messaging/
│   │   ├── channels.py       # ChannelId enum + ALLOWED_ROUTES (12 ルート)
│   │   ├── message.py        # Message, SendResult dataclass
│   │   └── bus.py            # MessageBus (S3*→S1 構造的隔離)
│   ├── eventlog/
│   │   ├── schema.py         # 26 event_type の pydantic モデル + Event envelope
│   │   ├── writer.py         # 単一 writer タスク (FIFO + 100ms append + fsync + 3-retry)
│   │   ├── reader.py         # read_all (同期) + iter_appended (非同期 tail)
│   │   └── replay.py         # replay() で 4 projection を再構成
│   ├── llm/
│   │   ├── types.py          # LLMRequest, LLMResponse, LLMProviderProtocol
│   │   ├── provider.py       # LiteLLM ラッパ (60s 二重防衛タイムアウト)
│   │   └── fake.py           # FakeLLMProvider + make_timeout_provider/make_error_provider
│   ├── systems/
│   │   ├── base.py           # System ABC + SubAgent (60s タイムアウト + Event_Log 記録)
│   │   ├── s1_worker.py      # 動的生成、specialization、current_assignments
│   │   ├── s2_coordinator.py # detect_conflict 純関数 + ack timeout 監視
│   │   ├── s3_allocator.py   # S1Pool.find_idle (idle 再利用優先) + 動的 spawn
│   │   ├── s3star_auditor.py # 30s timer + completion_signal 多重化
│   │   ├── s4_scanner.py     # 営業/リサーチ Sub_Agent + assessment 生成 + 3-retry 配送
│   │   └── s5_policy.py      # asyncio.gather による並行ディスパッチ (片側失敗非ブロック)
│   └── runtime/
│       ├── state.py          # TaskState enum + ReconstructedState + S1LifecycleEvent etc.
│       └── lifecycle.py      # Platform.create / start / shutdown / spawn_s1 + 構造検証
├── tests/
│   ├── unit/                 # 128 件
│   ├── property/             # 154 件 (Hypothesis ベース PBT)
│   └── integration/          # 3 件 (test_representative_scenario.py)
├── scripts/
│   └── smoke_run.py          # FakeLLMProvider で E2E スモーク確認
├── runs/                     # 実行時生成 (gitignore)
├── pyproject.toml
├── README.md
└── .gitignore
```

### 2.2 依存関係 (pyproject.toml)

- Python >=3.11
- `litellm`, `pydantic>=2`, `typer`
- 開発: `pytest`, `pytest-asyncio`, `hypothesis`

---

## 3. 設計の中核方針

### 3.1 アーキテクチャ判断

| 判断 | 理由 |
|---|---|
| **asyncio 単一プロセス** | I/O バウンド + タイミング要件 (500ms/1s/5s) が `asyncio.gather`/`asyncio.wait_for` で自然表現できる。マルチスレッド/マルチプロセスは導入しない |
| **JSONL = Source of Truth** | ランタイムキャッシュは Python オブジェクト、ただし状態の権威は常に JSONL 側。replay でランタイム状態が完全復元可能 (REQ 10.10) |
| **自前 Message_Bus + 静的許容テーブル** | ALLOWED_ROUTES (12 ルート) で定義外チャネルを構造的に拒否 (REQ 2.7)。S3* → S1 は (receiver_id, channel) キーで物理的に S3_Allocator のキューに届かない |
| **LLM 抽象化レイヤ** | LLMProviderProtocol で本番 (LiteLLM) とテスト (FakeLLMProvider) を切替。`LITELLM_PROVIDER` 環境変数 / `vsm.toml` でモデル切替 (REQ 3.7) |
| **System / Sub_Agent 二層構造** | System が VSM 上の役割をラップ、Sub_Agent が LLM プロンプト実行ユニット。1 System に 1〜64 Sub_Agent (REQ 1.4) |
| **Event_Log 単一 writer タスク** | FIFO 順 (REQ 10.8) + 100ms append (REQ 10.5) + fsync で耐障害性。3 回リトライ (REQ 10.6) |
| **S5 の並行ディスパッチ** | `asyncio.gather(send_to_s3, send_to_s4, return_exceptions=True)` で 1 秒以内 (REQ 6.4) と片側失敗非ブロック (REQ 6.5) を同時達成 |

### 3.2 Channel 一覧 (ALLOWED_ROUTES = 12 ルート)

| Channel | 双方向/単方向 | sender role | receiver role |
|---|---|---|---|
| S1-S2 | 双方向 | S1_WORKER ↔ S2_COORDINATOR | (両方向) |
| S1-S3 | 双方向 | S1_WORKER ↔ S3_ALLOCATOR | (両方向) |
| S3-S4 | 双方向 | S3_ALLOCATOR ↔ S4_SCANNER | (両方向) |
| S3-S5 | 双方向 | S3_ALLOCATOR ↔ S5_POLICY | (両方向) |
| S4-S5 | 双方向 | S4_SCANNER ↔ S5_POLICY | (両方向) |
| S3*->S1 | **単方向** | S3STAR_AUDITOR | S1_WORKER |
| S3*->S5(audit) | **単方向** | S3STAR_AUDITOR | S5_POLICY |

S3*_Auditor は S3_Allocator を経由しない (REQ 9.1)。Bus の (receiver_id, channel) キー構造で物理的に保証。

### 3.3 Event_Log スキーマ (26 event_type)

すべての event は共通エンベロープを持つ:
```json
{"ts": "2025-01-15T03:14:15.926Z", "run_id": "run-...", "event_type": "...", "seq": 42, "payload": {...}}
```

26 個の event_type と payload は `vsm/eventlog/schema.py` の `PAYLOAD_MODELS` に pydantic モデルとして登録済み。

### 3.4 17 Correctness Properties (Property-Based Testing)

design.md §Correctness Properties に 17 個の不変条件が定義され、Hypothesis ベース PBT で検証済み:

| # | Property | 関連 REQ | 検証ファイル |
|---|---|---|---|
| P1 | Channel rejection invariant | 2.7, 2.8 | test_message_bus.py |
| P2 | Channel delivery invariant | 2.1〜2.6, 2.9 | test_message_bus.py |
| P3 | Event SLA conformance | 1.5, 1.6, 2.9, 3.3, 4.6, 5.4, 6.5, 6.6, 7.4, 7.7, 8.7, 9.2, 9.4, 9.6, 10.5 | test_event_sla.py |
| P4 | Latency-bounded operation invariant | 5.2, 5.5, 5.7, 6.2, 6.3, 6.4 | test_operation_sla.py |
| P5 | Event_Log round-trip | 10.1, 10.9, 10.10 | test_event_log_replay.py |
| P6 | FIFO append order | 10.8 | test_event_log_fifo.py |
| P7 | Required field presence | 10.7, 10.2 | test_event_log_schema.py |
| P8 | S1 reuse vs instantiate dichotomy | 7.2, 7.3, 13.6 | test_s3_allocator.py |
| P9 | Conflict detection correctness | 8.2 | test_s2_conflict_detection.py |
| P10 | Mandatory systems verification | 1.7, 13.1, 13.2, 13.3 | test_lifecycle_verification.py |
| P11 | Bounded counts | 1.3, 1.4, 13.4, 13.5, 13.6 | test_bounded_counts.py |
| P12 | Tail filter semantics | 11.2, 11.3, 11.4 | test_cli_tail_filter.py |
| P13 | CLI input validation | 4.2, 4.5, 10.2, 11.7, 14.8 | test_cli_input_validation.py + test_ids.py |
| P14 | Audit schedule | 9.1 | test_audit_schedule.py |
| P15 | Concurrent dispatch resilience | 6.4, 6.5 | test_s5_dispatch_resilience.py |
| P16 | Retry semantics | 5.6, 10.6 | test_retry_semantics.py |
| P17 | Out-of-scope absence and rejection | 14.1〜14.8 | test_out_of_scope.py |

---

## 4. 実装の進行履歴

### 4.1 完了したタスク (84/84)

84 タスクすべて完了。タスクごとの詳細は `tasks.md` 参照。主要グループ:

1. **Project scaffolding** (Task 1)
2. **Foundation primitives** (Tasks 2-6): errors / ids / clock / config / Event_Log schema / writer / replay / reader
3. **Message_Bus** (Tasks 8.1-8.4)
4. **LLM Provider** (Tasks 9.1-9.3)
5. **System base + Lifecycle** (Tasks 10.1-11.3)
6. **6 つの具象 System** (Tasks 12.1-17.2): S5 / S4 / S3 / S2 / S3* / S1
7. **CLI 4 サブコマンド** (Tasks 19.1-22.2)
8. **統合テスト** (Tasks 24.1-26.1): 代表シナリオ 3 ケース
9. **README + Smoke** (Tasks 27.1-27.2, 28)
10. **オプション PBT 全 25 件** (`*` 付きタスク群) — Wave A・B で実装

### 4.2 PBT が発見した本物のバグ (3 件、すべて修正済み)

#### バグ #1: `Platform.shutdown()` の `RuntimeError`
- **発見**: Task 12.3 の `test_s5_dispatch_within_1s` 実行時
- **症状**: `RuntimeError: dictionary changed size during iteration`
- **原因**: S3 が `spawn_s1` で systems dict を mutate している最中に shutdown が iterate
- **修正**: `lifecycle.py::Platform.shutdown` で systems のスナップショット化

#### バグ #2: `_scope_guard` の sys.argv 依存
- **発見**: Task 19.3 の `test_out_of_scope.py` PBT
- **症状**: `runner.invoke(app, ["fsx"])` でも exit 5 を返さず exit 2 (Click 経由で sys.argv が更新されない)
- **原因**: `_scope_guard` callback が `sys.argv[1:]` のみを check、Typer の CliRunner は context 経由で渡す
- **修正**: カスタム `TyperGroup._ScopeGuardGroup` で `resolve_command()` を override、parser 層で intercept (cli.py)

#### バグ #3 (本日発見): subscribe-before-send レース条件
- **発見**: 自動動作確認 (`scripts/smoke_run.py`) 実行時
- **症状**: S3 が `spawn_s1` 直後に S1_S3 で assignment を送るが、S1 の `run()` ループが `subscribe` する前に send が走り `channel_rejected` が連発、S1 が assignment を受信できず `s1_completion` が出ない (整合性: 64 件すべて rejected)
- **連鎖症状**: S5 → S4 follow-up が無限ループ (REQ 5.7 の正常動作だが終了条件なし)
- **原因**: `Platform.start()` は `asyncio.create_task(s1.run())` するだけで、その時点で `run()` 最初の文 `bus.subscribe` はまだ実行されていない可能性がある
- **修正 1 (本体)**: `lifecycle.py` で
  - Run start 時の各必須 System に対して、`Platform.start()` 前にインバウンドチャネルを事前 subscribe (新規定数 `_ROLE_INBOUND_CHANNELS`)
  - `Platform.spawn_s1` で `s1.start()` 前に S1 の 3 チャネル (S1_S3, S1_S2, S3STAR_TO_S1) を事前 subscribe
- **修正 2 (テスト)**: SLA テストで S5 ループを停止して S4↔S5 feedback の無限ループを回避 (`test_operation_sla.py`, `test_retry_semantics.py`)。`test_s5_dispatch_resilience.py` で既に確立されたパターン

### 4.3 動作確認の最終結果

| レベル | 内容 | 結果 |
|---|---|---|
| 0 | 全テスト実行 (285 件) | ✅ 285/285 passed in 46s |
| 1 | CLI スモーク (`vsm --help`, `vsm fsx`, `vsm submit ""`, `vsm status nonexistent`) | ✅ exit 0/5/2/2 + 適切な stderr |
| 3 | E2E スモーク (`scripts/smoke_run.py`, FakeLLMProvider) | ✅ `s1_completion` 観測、全 6 役割 event 発生 |
| 4 | `vsm status` / `vsm replay` の出力 | ✅ 正常 |

レベル 2 (実 LLM 経由) はユーザー側で API キー設定して `vsm submit` するだけで実行可能。

---

## 5. 開発環境

### 5.1 セットアップ手順

```bash
cd /Users/rkhashim/Kiro/Others

# Python 3.13 (Homebrew) で venv 作成
/opt/homebrew/opt/python@3.13/bin/python3.13 -m venv .venv
source .venv/bin/activate

# 依存インストール
pip install -e .

# テスト実行
pytest -q
```

### 5.2 LLM 接続 (実 LLM テスト用)

```bash
# OpenAI
export LITELLM_PROVIDER="openai/gpt-4o-mini"
export OPENAI_API_KEY="sk-..."

# Anthropic
export LITELLM_PROVIDER="anthropic/claude-3-5-haiku-20241022"
export ANTHROPIC_API_KEY="sk-ant-..."

# Bedrock (Amazon 社内環境)
export LITELLM_PROVIDER="bedrock/anthropic.claude-3-5-haiku-20241022-v1:0"
export AWS_REGION="us-west-2"
```

### 5.3 主要 CLI コマンド

```bash
vsm submit "<description>" [-f file ...]    # タスク投入
vsm status <run_id>                          # 現状サマリ (Tasks + Systems)
vsm tail <run_id> [--system X] [--channel Y] # 追従観測
vsm replay <run_id>                          # 全イベントを timestamp 順に
```

### 5.4 Exit Code

| code | 意味 |
|---|---|
| 0 | 正常終了 |
| 1 | 内部例外 (未分類) |
| 2 | CLI 入力バリデーション違反 (description / file / run_id) |
| 3 | 構造制約違反 (必須 System 不足) |
| 4 | Run ディレクトリ / Event_Log 作成失敗 |
| 5 | スコープ外機能要求 (REQ 14.8) |
| 6 | 代表シナリオの 1800 秒タイムアウト (REQ 12.9) |

---

## 6. 構想ドキュメントとの位置関係

ユーザーが提供した構想ドキュメント (Mitobe 2025) は VSM + FSX + 5 つの構成概念 (層横断的自律性、可逆委任、非序列的参加、勾配的公共性、共有剰余) を統合する組織アーキテクチャを提案している。

本 PoC は **構造 (VSM)** の部分のみを実装しており、以下は意図的に未実装:

| 構想要素 | 本 PoC での扱い |
|---|---|
| VSM (Viable System Model) | ✅ 実装 (S1〜S5 + S3*) |
| 5 つの構成概念 | ⚠️ 一部のみ (人間-AI 関係の非序列的構造のみ; 実質的自律性、勾配的公共性、共有剰余は未実装) |
| FSX (Future-State Expansion) | ❌ 完全に除外 (REQ 14.1) |
| 公共性測定 | ❌ 除外 (REQ 14.2) |
| 共有剰余配分 | ❌ 除外 (REQ 14.3) |
| 人間の層横断介入 (テンポラル・インターフェース) | ❌ 除外 (REQ 14.4) |
| 内部分化・外部包摂 | ❌ 除外 (REQ 14.5) |
| セミステートフル記憶混合 | ❌ 除外 (REQ 14.6) |

この境界は `tasks.md` 完了時点の構想 (Version 0.1) と整合している。後続イテレーションで段階的に拡張する余地がある。

---

## 7. 設計上の重要な前提

### 7.1 ステートレス LLM 前提 (構想ドキュメント Part 5)

本 PoC は **「現行 LLM はステートレス」** という前提に依拠している:
- 各 `litellm.acompletion` 呼び出しは過去会話履歴を持たない
- Sub_Agent.respond は単発の prompt のみ送信
- 「LLM の可換性」(構想 Part 5.1 von Foerster の操作的閉鎖性) が委任の根拠

技術進展で LLM がステートフル化した場合 (REQ 14.6 で SHALL NOT としている記憶混合等)、本枠組みの再検討が必要 (構想 Part 5.2)。

### 7.2 タスクコンテキストの転送

CLI から各 System への context 転送は以下 5 層で構成:

1. **CLI → Platform**: `task_payload` dict (task_id, run_id, description, file_paths, submitted_at)。**ファイル内容は含まれず file_paths のみ** (将来拡張ポイント)
2. **System 間**: Message_Bus 経由の `Message.payload: dict`。各チャネルで運ばれる schema は `vsm/messaging/message.py` と `vsm/eventlog/schema.py` で定義
3. **System → LLM**: `Sub_Agent.respond(prompt, context)`。各 System が業務知識 + 受信 payload を prompt 文字列に圧縮する責務を持つ
4. **Event_Log**: `runs/{run_id}/events.jsonl`。replay() で 4 projection (tasks, s1_lifecycle, channel_events, audit_findings) として完全再構成可能
5. **System 内部状態**: 揮発的・プロセス内 (current_assignments, _pending_acks 等)。Event_Log から再構成可能

### 7.3 動作確認の戦略

- **テスト中心**: 285 件の自動テスト (FakeLLMProvider 使用、LLM API キー不要) で全 17 Property を検証
- **実 LLM**: ユーザーが必要に応じて `vsm submit` で実行 (代表シナリオの自然言語動作観察)
- **スコープ外確認**: PBT (Property 17) で OUT_OF_SCOPE_NAMES への CLI rejection を回帰検出

---

## 8. 引き継ぎ時の注意点

### 8.1 リポジトリ push に関する制約

- `https://github.com/yujimtb/Nanihold_OS.git` への push は Amazon の Code Defender (社内ポリシー) でブロック済み
- ユーザーは push を諦めて、ローカル維持の方針を採用 (会話履歴より)
- 必要に応じて `git-defender --request-repo --reason 3` で承認リクエスト可能

### 8.2 既知の動作上の癖

- `Platform.shutdown()` は systems dict の snapshot を取って iterate (バグ #1 の修正後)
- S5 を含む完全な run loop で long-lived 動作させると、S4 → S5 → S4 の followup が永久に回り続ける (REQ 5.7 の正常動作)。CLI submit は 1800 秒タイムアウトで止める設計
- `vsm tail` は `asyncio.Queue.put_nowait` ベースで back-pressure を発生させない。極端な burst で QueueFull が出たら設計再検討
- macOS の zsh で `vsm submit` のとき、description が空文字 `""` の場合は zsh 自体が引数を消費する場合あり → `vsm submit ''` のように quote 必須

### 8.3 拡張ポイント (構想と整合する将来作業)

| 拡張 | 関連 REQ / 構想 |
|---|---|
| `task_payload` にファイル内容を含める | REQ 4.3 (現状 file_paths のみ) |
| 分散トレース ID で同一 Task 由来のメッセージを束ねる | (新規) |
| Sub_Agent ごとの軽量 context window | REQ 14.6 と矛盾しない範囲で |
| Replay からの動的 Run 再開 (`vsm replay --resume`) | (新規) |
| FSX 数値最適化 | 構想 Part 2.2、REQ 14.1 のスコープ拡張 |
| 公共性測定 | 構想 Part 3.5、REQ 14.2 のスコープ拡張 |
| 人間の層横断介入 (サブVSM デプロイ) | 構想 Part 4.2、REQ 14.4 のスコープ拡張 |
| 動的内部分化・外部包摂 | 構想 Part 4.3、REQ 14.5 のスコープ拡張 |

---

## 9. クイックリファレンス

### 9.1 すぐに動作確認したい場合

```bash
cd /Users/rkhashim/Kiro/Others
source .venv/bin/activate

# 全テスト
pytest -q                                          # 285/285 passed in ~46s

# CLI スモーク
vsm --help                                         # 4 サブコマンド表示
vsm fsx                                            # exit 5, scope guard
vsm submit ""                                      # exit 2, validation
vsm status nonexistent-run                         # exit 2, REQ 11.7

# E2E スモーク (FakeLLMProvider)
python scripts/smoke_run.py                        # ~3s で完了

# 実 LLM (要 API キー)
export LITELLM_PROVIDER="openai/gpt-4o-mini"
export OPENAI_API_KEY="sk-..."
vsm submit "Hello, what is VSM?"
```

### 9.2 主要なドキュメント

- `requirements.md` — 14 個の Requirement (EARS パターン、タイミングバウンド完備)
- `design.md` — アーキテクチャ + 17 Properties + 6 Mermaid 図
- `tasks.md` — 84 タスク (全 completed) + 25 オプション PBT
- `README.md` — Quick Start + MVP Scope Boundaries + Exit Code 表

### 9.3 トラブルシューティング

| 症状 | 対処 |
|---|---|
| `pip install -e .` 失敗 | Python 3.11+ 必須。Homebrew の `python@3.13` 推奨 |
| pytest が `ModuleNotFoundError: vsm` | `pip install -e .` 忘れ or venv 未 activate |
| `vsm submit` が `LITELLM_PROVIDER not configured` | env var 設定 or `vsm.toml` に `[llm] provider = "..."` 追加 |
| 実 LLM で 1800 秒タイムアウト (exit 6) | LLM 応答遅延。`vsm tail` で `llm_invocation` を確認 |
| `runs/` 既存エラー | `Platform.create` は `mkdir(exist_ok=False)`。新 run_id で再実行 |
| `vsm tail` が応答しない | tail は append を待ち続ける。Ctrl+C で抜け、完了済みなら `vsm replay` を使う |
| zsh で `vsm submit ""` の `""` が消える | `vsm submit ''` (single quote) を使う |

---

## 10. 連絡先・参照

- **ユーザー**: 三戸部友治 (構想ドキュメント著者、リポジトリ所有者: `yujimtb`)
- **構想ドキュメント**: ユーザーから提供 (Version 0.1, 構想段階)
- **Mitobe 2025**: 構想の理論的基盤論文 (https://docs.google.com/document/d/1xiQxQBO-1tD6A_02BQ6R7uhgQxMYKzXnMLQwntyuSN8/edit)

---

**このドキュメントは Nanihold OS の MVP 実装が完了した時点 (2026-05-24) のスナップショットです。後続作業で変更があった場合は、本ファイルも合わせて更新してください。**
