# 実装状況とスコープ

[architecture.md](architecture.md) で定めた構造に対する現在の実装到達状況、Tool 群の実装状況、
および MVP からの拡張スコープ(REQ 14.x)をまとめる。

> 出典は旧 README の実装状況節と、作業文書 `docs/archive/refactor_20260608.md` の
> 「現在の実装状況」「未決論点」節。

---

## 1. アーキテクチャ層の実装状況

`architecture.md` の基礎方針に対して、現在の実装は以下の状態である。

| 項目 | 実装 |
|---|---|
| Architecture / Role / Agent / Tool / Node 分離 | `vsm.architecture`, `vsm.roles`, `vsm.agents`, `vsm.tools`, `vsm.nodes` に分離済み。Architecture 層は VSM 構造、Role 層は契約、Agent 層は一時実行主体、Tool 層は具体手続き、Node 層は責任・履歴・権限・状態を扱う。 |
| EventEnvelope v1 | `vsm.eventlog.schema.Event` と `vsm.architecture.events.EventEnvelope`。`event_id`, `stream_id`, `stream_version`, `schema_version`, `correlation_id`, `causation_id` を持つ。 |
| Projection checkpoint | `vsm.architecture.projections.ProjectionCheckpoint`。処理済み `event_id` を保持して同一イベント再適用を防ぐ。 |
| Node / u-VSM / NodeRunState | `vsm.nodes.model.Node`, `DifferentiationLevel`, `NodeRunState`。すべての Node を u-VSM として扱い、Run 固有状態は `NodeRunState` に分離。CLI セッション参照は backend ごとに Run 内・同一 Node 内だけで保持し、Run 終了時に破棄する。`NodeSource` により `terminable=False` は config 由来の Node のみに制限。 |
| static / live topology | `vsm.runtime.topology.StaticTopologyEntry`, `LiveTopology`。Event_Log 由来の `node_created`, `node_differentiated`, lifecycle event を反映。 |
| ParentAuthority / Lease | `vsm.authority.ParentAuthority`, `Lease`。分化上限、Tool effect 制限、外部資源 lease を表す。 |
| ToolEffect / idempotency | `vsm.tools.ToolEffect`, `ToolInvocation`。`EXTERNAL_WRITE` と `CONTROL` は `idempotency_key` 必須。 |
| Tool facade | `LLMCallFacade`, `CodexRunFacade`, `SpawnChildFacade`, `DifferentiationFacade`, `SearchPastSubtasksFacade`, `CoordinationFacade`, `AlgedonicFacade`, `EscalationFacade`, `HumanReviewFacade`, `NodeControlFacade`。 |
| S2 AI 調停 | `request_coordination` の issue / participants / claims を S2 AgentRuntime が判断し、`coordination_decided` に決定と理由を記録する。`[coordination] ai_deliberation` で無効化可能。 |
| Algedonic / Consortium | 任意 Node / Human から S5 へ直送する `ALGEDONIC` channel、S5 の対応選択、階層非依存のラウンド制 Consortium、人間 statement timeout の `proceed` / `abort` を実装済み。AI 参加者の context view は Platform が `ContextViewBuilder` を hook adapter として注入する。 |
| サブ VSM デプロイ | `differentiate` Tool と `LiveTopology` により、親 Authority の範囲内で child Node を u-VSM として展開する基礎機能を実装済み。 |
| Role / Agent / Execution | `RoleSpec`, `AgentSpec`, `PromptTemplate`, `Execution`。Spec versioning と Agent / Tool 実行単位を明示。 |
| Memory / Graph / Telemetry | `ContextView`, `TaskSummary`, `GraphProjection`, `TelemetryCorrelation` を実装。`ContextViewBuilder` は Node の直近イベント、親 directive、直接 child の TaskSummary、参照 Artifact を短い日本語ビューへ決定論的に射影する。S1 完了時は規則ベースの TaskSummary を Run 配下の `memory/task-summaries.jsonl` に登録する。 |
| Run Budget / quota recovery | `[budget]` / `[budget.roles]` を Authority と NodeRunState に注入し、AgentResult の input/output/cache-read token と wall clock を累算・呼出前強制する。quota 枯渇時は Node を休眠し、reset 時刻に保留 Message を再投入して自動復帰する。resume 失敗時の物理再試行は論理呼び出し内に閉じ、成功結果への課金とイベント発行を各1回にする。Quota/Algedonic の suspend は共通 lifecycle 操作を使う。 |

### まだ full runtime policy として有効化していないもの

```text
dynamic differentiate の自律実行
request_escalation の親 Node 判断ループ
lease timeout 後の recovery policy
Run 間 budget accounting
Secret store / Artifact store の具体実装
OpenTelemetry exporter 連携
```

---

## 2. Tool 群の実装状況

共通契約として `ToolEffect`, `ToolSpec`, `ToolInvocation` を実装済みで、`EXTERNAL_WRITE` と
`CONTROL` の `idempotency_key` 必須制約もコード上で検証する。

| Tool | 現状 |
|---|---|
| `llm_call` | `LLMCallFacade`, `LLMCallRequest`, `LLMCallResult` を実装済み。`ToolEffect.EXTERNAL_READ` の `ToolInvocation` として LLM provider を呼び出し、replay 時は `tool_completed` の保存済み result を `ReconstructedState.tool_results` から参照する。 |
| `codex_run` | `CodexRunFacade`, `CodexRunRequest`, `CodexRunPolicy`, `CodexRunResult` を実装済み。Codex CLI を外部プロセス実行 Tool として呼び出し、`ToolEffect.EXTERNAL_READ` / `EXTERNAL_WRITE` / `CONTROL`、`idempotency_key`、`ParentAuthority.filesystem_scope`、sandbox allow-list による policy 制約を検証する。全 System role の `RoleSpec.allowed_tools` に `codex_run` をアタッチし、`agent_attached` event にも tools として記録する。現時点では VSM 内部 Tool であり、専用 CLI サブコマンドはない。 |
| `claude_code_run` | 未実装。`codex_run` と同じ外部プロセス実行 Tool の一種として扱う予定。 |
| `web_crawl` | 未実装。`ToolEffect.EXTERNAL_READ` と ParentAuthority の network scope による制約を前提に導入する。 |
| `file_io` | 未実装。`ToolEffect.PURE_READ` / `LOCAL_WRITE` と ParentAuthority の filesystem scope による制約を前提に導入する。 |
| `spawn_child` | `SpawnChildFacade`, `SpawnChildRequest`, `SpawnChildResult` を実装済み。`CONTROL` ToolInvocation を生成し、`Platform.spawn_s1` では facade 経由で実際の `S1Worker` 生成、bus 事前購読、`start()`、`tool_completed` 記録まで行う。 |
| `differentiate` | `DifferentiationFacade` と `DifferentiationRequest` を実装済み。`ParentAuthority.may_differentiate_to` を検証し、冪等な `CONTROL` ToolInvocation を生成する。 |
| `search_past_subtasks` | `TaskSummaryIndex`, `SearchPastSubtasksFacade`, `IndexedTaskSummary` を実装済み。`TaskSummary` を JSONL の永続 index に保存し、`SearchScope` / query / limit で検索する `PURE_READ` ToolInvocation を生成する。 |
| `request_coordination` | `CoordinationFacade` と `CoordinationRequest` を実装済み。`coordination_key` を `idempotency_key` とする `CONTROL` ToolInvocation を生成する。 |
| `raise_algedonic` | `AlgedonicFacade` と `AlgedonicRequest` を実装済み。任意 Node または Human から階層をバイパスして S5 に配送し、設定に応じて人間向け通知イベントも記録する。 |
| `request_escalation` | `EscalationFacade` と `EscalationRequest` を実装済み。`escalation_key` を `idempotency_key` とする `CONTROL` ToolInvocation を生成する。 |
| `request_human_review` | `HumanReviewFacade` と `HumanReviewRequest` を実装済み。`HumanAgent` を任意に指定し、人間レビュー要求を `ToolEffect.HUMAN` の `ToolInvocation` として記録する。 |
| `terminate_node` | `NodeControlFacade.terminate_node` を実装済み。`CONTROL` effect、`ParentAuthority.termination_authority`、`Node.terminable`、Node lifecycle transition を検証する。 |
| `suspend_node` | `NodeControlFacade.suspend_node` を実装済み。`CONTROL` effect と Node lifecycle transition を検証し、`NodeStatus.SUSPENDED` へ遷移させる。 |
| `resume_node` | `NodeControlFacade.resume_node` を実装済み。`CONTROL` effect と Node lifecycle transition を検証し、`NodeStatus.RUNNING` へ遷移させる。 |

---

## 3. Current Scope and Roadmap

Nanihold OS は MVP 境界を越え、VSM ランタイムとしての実装範囲を拡張中である。S1_Worker は
LLM 応答を `s1_completion` の `result` に記録し、S1〜S5 + S3* の各 System、Event_Log、
Node / ParentAuthority、Tool facade、Projection、Role / Agent / Execution の基礎モデルを
組み合わせて実行される。

主要な実装状況は以下の通り(REQ 番号は旧 MVP スペックに由来する識別子)。

- **REQ 14.1**: FSX (Future-State Expansion) の数値最適化・目的関数評価は未実装。
- **REQ 14.2**: 公共性測定および勾配的公共性評価は未実装。
- **REQ 14.3**: 共有剰余の配分ロジックは未実装。
- **REQ 14.4**: サブ VSM デプロイは機能として実装済み。人間の層横断介入とテンポラル・
  インターフェースは今後の拡張対象。
- **REQ 14.5**: 動的な内部分化・外部包摂による再帰的成長は、`differentiate` /
  `request_escalation` などの Tool facade と Node / ParentAuthority の基礎モデルを実装済み。
  自律運用するランタイムポリシーは段階的に有効化する。
- **REQ 14.6**: S2_Coordinator によるセミステートフル記憶の集団的混合は未実装。
- **REQ 14.7**: ローカル HTTP で到達可能な Web UI ダッシュボードを実装済み。タスク投入、
  Run 履歴、リアルタイム進捗、追加指示、停止、結果表示を扱う。

コード実行、ファイル編集、外部プロセス実行は短期ロードマップの対象。これらは
ToolEffect / ToolInvocation の effect 境界、idempotency key、ParentAuthority / Lease による
権限管理と組み合わせて、安全な実行単位として導入する方針である。

永続的な会社運用と Run 間の長期記憶は、Node と Event_Log を中心に扱う方向で設計を進めている。
現時点では FSX、公共性評価、共有剰余配分などの評価・分配アルゴリズムが主な未実装領域である。

製品化に向けた段階的な実行計画は [roadmap.md](roadmap.md) を参照。

---

## 関連ドキュメント

- [architecture.md](architecture.md) — 設計リファレンス(本書が対応する構造の定義)
- [roadmap.md](roadmap.md) — 製品化までの実行計画
