# Design: Agent Runtime と VSM 完成機能

proposal.md の決定事項に対する詳細設計。実装ウェーブは tasks.md を参照。

---

## 1. AgentRuntime 抽象(vsm/agents/runtime.py 新設)

現行の `LLMProviderProtocol`(prompt→text の単発)を包含する上位抽象。

```python
@dataclass(frozen=True)
class AgentRequest:
    prompt: str
    context_view: str | None = None      # Node側が構築した文脈(§4)
    session_ref: str | None = None       # 再開するセッションID(§4)
    workdir: Path | None = None          # CLIエージェントの作業ディレクトリ
    model: str | None = None             # バックエンド既定の上書き
    timeout_seconds: float | None = None # バックエンド既定の上書き

@dataclass(frozen=True)
class AgentResult:
    text: str
    tokens_in: int
    tokens_out: int
    tokens_cache_read: int               # キャッシュ読取(削減効果の観測用)
    latency_ms: int
    model: str
    backend: str                         # "claude-code" | "codex" | "litellm" | "fake"
    session_ref: str | None              # 継続用セッションID(次回のresume材料)
    quota_exhausted: bool = False        # リミット検知(§6)
    quota_reset_at: datetime | None = None

class AgentRuntimeProtocol(Protocol):
    backend_name: str
    async def invoke(self, request: AgentRequest) -> AgentResult: ...
```

### バックエンド実装(vsm/agents/backends/)

| backend | 実装 | トークン/セッションの取得 |
|---|---|---|
| `claude-code` | `claude -p --output-format json [--resume <sid>]` をサブプロセス実行 | 出力JSONの `usage.input_tokens` / `output_tokens` / `cache_read_input_tokens`、`session_id` |
| `codex` | `codex exec --json -m <model> -c model_reasoning_effort=<effort>`(継続時は `codex exec resume <sid>`)、プロンプトはstdin | JSONLイベントの token_count 系、thread/session id イベント |
| `litellm` | 既存 `vsm/llm/provider.py` のアダプタ(後方互換・API利用したい場合の退避先) | 既存どおり |
| `fake` | 既存 `FakeLLMProvider` 相当のアダプタ。全テストの既定 | 決定論 |

実装上の注意:
- サブプロセスは `asyncio.create_subprocess_exec` + `process_factory` 注入(既存 `CodexRunFacade` と同型。テストで差し替え可能に)。stdinは明示的に閉じるか渡し切る(開きっぱなしにするとCLIが入力待ちで固まる)。
- CLI が JSON を返せなかった場合(クラッシュ・非JSON出力)は `AgentRuntimeError`(新設、`LLMProviderError` の兄弟)に正規化。
- **タイムアウト**: 現行の一律60秒はCLIエージェントに不適合。バックエンド別既定(claude-code/codex: 1800秒、litellm/fake: 60秒)+ 設定で上書き。`SubAgent.respond` の `asyncio.wait_for` はこの値を使う。
- レート制限検知(§6): claude は結果JSON/stderr の limit 文言と exit code、codex は JSONL のエラーイベント/stderr を判定し `quota_exhausted=True` と(取得できれば)`quota_reset_at` を返す。**例外にせず結果として返す**(呼び出し側が休眠処理を行うため)。

### SubAgent の配線変更(vsm/systems/base.py)

- `SubAgent._llm: LLMProviderProtocol` → `SubAgent._runtime: AgentRuntimeProtocol` に置換。
- Event_Log 互換: 既存イベント名 `llm_invocation` / `llm_timeout` / `llm_error` は維持し、payload に `backend` / `session_ref` / `tokens_cache_read` を追加(schema_version を上げる)。replay 互換を壊さない。
- ロール別バックエンド解決は Platform.create 時に行い、System ごとに適切な runtime を注入する(現行は全System共有の1プロバイダー → **ロール別に異なる runtime インスタンス**へ)。

## 2. 設定の一元化(vsm.toml)

`load_config` を拡張。env は最小限(秘密と一時上書きのみ)、構造は vsm.toml に集約。

```toml
[llm]                       # 後方互換(litellmバックエンド用)
provider = "openrouter/deepseek/deepseek-v4-flash"

[agents]
default_backend = "claude-code"

[agents.backends.claude-code]
bin = "claude"              # PATH解決。env CLAUDE_BIN で上書き可
model = ""                  # 空=サブスク既定モデル
timeout_seconds = 1800

[agents.backends.codex]
bin = "codex"
model = "gpt-5.6-sol"
reasoning_effort = "high"
timeout_seconds = 1800

[agents.roles]              # ロール→バックエンド(1行で差し替え)
S5_POLICY = "claude-code"
S4_SCANNER = "claude-code"
S3_ALLOCATOR = ""           # 空=決定論のまま(LLM不使用)
S2_COORDINATOR = "claude-code"
S3STAR_AUDITOR = "claude-code"
S1_WORKER = "codex"

[session]
resume_within_run = true    # Run内・同一Node内のセッション再開(§4)

[budget]                    # §5
run_tokens = 2000000
run_wall_clock_seconds = 7200
[budget.roles]              # ロール別エンベロープ(比率または絶対値)
S1_WORKER = { tokens = 500000, wall_clock_seconds = 1800 }

[quota]                     # §6
suspend_on_exhausted = true
fallback_resume_minutes = 60   # reset時刻が取れないときの再試行間隔
weekly_fallback_resume_minutes = 360

[coordination]              # §7
ai_deliberation = true

[algedonic]                 # §8
notify_human = true

[consortium]                # §9
default_rounds = 2
human_participation = "invited"   # invited | required | none
human_timeout_seconds = 3600
human_timeout_policy = "proceed"  # proceed | abort

[web]
concurrent_agents_max = 3   # サブスクの並行CLIプロセス上限
```

## 3. Budget(architecture.md §14 の実装)

- **注入**: Platform の `_attach_system_node` で `[budget]` から `ParentAuthority.budget_envelope` と `NodeRunState.budget` を実際に設定(現在は空)。
- **記録**: `SubAgent.respond` 成功時に `AgentResult` から `budget_consumed` イベントを append(payload: node_id, tokens_in/out/cache_read, wall_clock_ms, 累計)。`NodeRunState.cost_consumed` に累算。wall clock はエージェント呼び出しの実測 latency と Node の RUNNING 時間の両方を持つ。
- **強制**: 呼び出し前チェックで超過なら Tool 拒否 + `request_escalation` 発行(仕様どおり)。拒否は `budget_exceeded` イベントとして残す。
- **表示**: `vsm status <run_id>` に Node別トークン/時間消費、`vsm runs` に Run合計。Web API `/api/runs/{id}/budget` で公開(§10)。

## 4. トークン削減(2層)

**第1層: context view 構築(vsm/memory/builder.py 新設、設計準拠)**
- `ContextViewBuilder.build(node_id, run_id) -> str`: Node の直近イベント要約 + 親からの directive + 直接 child の TaskSummary + 参照 Artifact を、テンプレートで**短い日本語ビュー**に組み立てる。
- S1 完了時に TaskSummary を必ず生成し(まずは応答からの規則ベース抽出でよい)、既存 `TaskSummaryIndex`(search_past_subtasks)に登録する。
- SubAgent 呼び出し時、prompt には「役割 + 今回の指示」だけを入れ、履歴は context_view に隔離する(再開セッションでは context_view を省略できる)。

**第2層: セッション再開(Run内・Node内限定)**
- `NodeRunState.session_refs: dict[backend, str]` を追加。同一 Run・同一 Node の2回目以降の invoke で `session_ref` を渡す。
- 成功時に返ってきた `session_ref` を更新。resume 失敗(セッション消滅)は**エラーにせず**新規セッションにフォールバックし、context_view を full で渡す。
- Run 終了でセッション参照は破棄(architecture.md §17 との折衷: Agent 内部文脈は Run を越えない)。

## 5〜6. クォータ自動復帰(vsm/runtime/quota.py 新設)

- `AgentResult.quota_exhausted` を受けた System は、自 Node を `NodeStatus.SUSPENDED` に遷移(`quota_exhausted` イベント、reset 予定時刻付き)。
- `QuotaMonitor`(Platform 常駐タスク)が reset 時刻(不明なら fallback 間隔)に `resume_node` 相当の遷移 + 保留中の作業を再投入。イベント: `quota_resumed`。
- 保留作業の再投入は「suspend 時に処理中だった Message を Node 単位の保留キューに退避 → resume 時に再送」で実現(メッセージ消失バグの既知領域なのでテスト必須)。

## 7. S2 調停の AI 化

- `request_coordination` の受け口として S2Coordinator の run loop に調停ハンドラを追加: 係争内容 + 当事者の主張(payload)を S2 の AgentRuntime に渡し、`coordination_decided`(決定 + 理由)を返す。
- `[coordination] ai_deliberation = false` なら現行の記録のみ動作に戻る。

## 8. Algedonic channel

- `ChannelId.ALGEDONIC` を新設。**どの Node からでも**発信可能な `raise_algedonic` Tool facade(severity: pain | pleasure、reason、source_node_id)。
- Bus は階層をバイパスして **S5 直行 + Human 通知**(WebUI アラート、`[algedonic] notify_human` で制御)にルーティング。
- S5 ハンドラ: 即時に `algedonic_handled` を発行し、対応(発生源の suspend / Consortium 招集 / 人間へのエスカレーション)を選ぶ。AI 化された S5 は AgentRuntime で判断し、判断根拠をイベントに残す。
- **Human も発信者になれる**: WebUI ボタンと API(§10)から algedonic を投げ込める。
- イベント: `algedonic_raised` / `algedonic_handled`。

## 9. Consortium(合議体、階層非依存)

- `vsm/runtime/consortium.py` 新設。**参加者は Node 参照のリスト**(役割固定にしない)+ 人間参加者。これにより最上位(root の S3-S4-S5 + Human)でも、任意の u-VSM 内部でも同じ機構が使える。
- プロトコル: 招集(`consortium_convened`: subject, participants, convener)→ ラウンド制で各参加者が意見表明(`consortium_statement`、AI 参加者は各自の AgentRuntime + context view で発言)→ 招集者(通常その階層の S5)が総合して `consortium_decided`(決定 + 理由 + 反対意見の要約)。
- **Human 参加**: `human_participation = invited` なら人間の statement 枠を設け、WebUI / API から投稿できる。`human_timeout_seconds` 経過時は `human_timeout_policy` に従い続行または中止。人間待ちの間、当該 Consortium は WAITING 状態としてダッシュボードに出す。
- 招集トリガ(初期実装): (a) S5 が重要 directive の前に任意招集、(b) algedonic 受信時、(c) 人間が API/WebUI から招集。

## 10. 指示 API と CLI(外部エージェントからの操縦)

FastAPI(vsm/web)に追加。ローカル利用前提(認証は当面なし。バインドは 127.0.0.1)。

```
POST /api/runs                          # Run 投入(goal, constraints, budget上書き)
POST /api/runs/{run_id}/instructions    # 追加指示(target_node 省略時は S5 宛)
POST /api/runs/{run_id}/algedonic       # 人間からの痛覚信号
POST /api/consortium/{consortium_id}/statement   # 人間の合議参加
GET  /api/runs/{run_id}/topology        # ライブ組織図(§11 のデータ源)
GET  /api/runs/{run_id}/budget          # 予算消費
```

- 追加指示は `instruction_received` イベント + 対象 Node への Message として配送。
- CLI: `vsm instruct <run_id> "<text>" [--node <id>]` を追加(Claude Code / Codex はこれか curl を使う)。

## 11. WebUI: ライブ組織図と介入

- 新ビュー「組織図」: `/api/runs/{id}/topology` を(SSE またはポーリングで)購読し、Node ツリーを描画。**Node カードに表示**: 役割 / バックエンドとモデル / 状態(色分け: RUNNING・IDLE・SUSPENDED・WAITING) / 現在の活動(直近 tool_invoked / llm_invocation の要約) / 誰の指示か(親 directive / decision_id / instruction 由来) / 予算消費バー。
- **介入操作**: suspend / resume / terminate、追加指示の投入、algedonic 発信、Consortium への statement 投稿、human review への応答。
- topology API は Event_Log からの projection(node_created / node_differentiated / lifecycle / agent_attached / llm_invocation / budget_consumed)として実装し、再構成可能性を保つ。

## 12. テスト方針

- 既存 334 テストは FakeRuntime 既定で全部緑のまま。
- 新規: バックエンドのサブプロセスは process_factory 注入でモックし、JSON/JSONL パーサ・quota 検知・resume フォールバック・budget 強制・suspend/resume・consortium プロトコル・API エンドポイントを決定論テスト。
- live マーカー(既存 `live_llm` に加え `live_agent`)で実 CLI の煙テストを分離。
