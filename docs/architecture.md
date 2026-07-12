# アーキテクチャ

Nanihold OS の構造・責務・履歴・権限・実行境界に関する正式な設計リファレンスです。

> この文書は、作業文書 `docs/archive/refactor_20260608.md`(リファクタリング基礎文書 改訂版)と
> 旧 README のアーキテクチャ節を統合して正式化したものです。設計指針の出典としては
> アーカイブ版を残していますが、現行の正は本文書です。実装の到達状況は
> [implementation-status.md](implementation-status.md) を参照してください。

---

## 0. 位置づけ

本書は Nanihold OS を「常時稼働する VSM 実行基盤」として扱うための構造の取り決めを定める。
実装手順ではなく、構造、責務、履歴、権限、実行境界の契約を記述する。

VSM 自体は長期稼働する前提とし、**Run は Platform 全体の起動停止ではなく**、外部入力、
業務依頼、定期処理、監査要求などに対応する実行・観測・会計の単位として扱う。永続する
責任、履歴、権限、状態は Node が保持し、Agent は Execution ごとに生成される一時的な
実行主体として扱う。

---

## 1. 基本原則

**Architecture / Role / Agent / Tool / Node を分離する。**

- Architecture は VSM の骨格を表す。
- Role は Node の責任契約を表す。
- Agent は Role を一時的に実行する主体である。
- Tool は具体的な手続きである。
- Node は責任、履歴、権限、状態を保持する永続的な単位である。

**履歴は Node に帰属し、Agent はステートレスとする。** Agent は Execution ごとに生成され、
応答後に破棄される。文脈は Node の履歴と Projection から構成される view として Agent に渡す。

**判断は局所化する。** ただし、必要な場合は理由を Event_Log に記録したうえで、親 Node、S4、
または許可された knowledge index へ検索範囲を広げられる。

**Node 型は単一にする。** StaticNode / DynamicNode は実装型ではなく、Node 属性の組み合わせに
対する通称である。

---

## 2. 中核概念

```text
Run:
  外部入力、業務依頼、定期処理、監査要求に対応する実行、観測、会計の単位。
  Run の終了は StaticNode の停止を意味しない。

Node:
  組織上の責任、履歴、権限、状態を持つ永続的な単位。

NodeRunState:
  Node が特定 Run で持つ status, budget, cost_consumed, context_view, output を表す。

Execution:
  ある Node が、ある Run において Agent を起動し、判断または Tool 実行を行う一回の処理単位。

Agent:
  Role を一時的に実行する主体。LLM、Codex、Claude Code、人間を同じ抽象で扱う。
```

---

## 3. 四層構造

### Architecture 層

VSM の構造を表す。System、Channel、Message、Event_Log、Node Tree、coordination graph、
再帰構造を扱う。LLM や Codex の存在を知らない。

### Role 層

Node の VSM 上の位置に紐づく契約定義である。YAML / JSON として保持する。

```text
Role:
  id
  vsm_position
  responsibility
  input_schema
  output_schema
  allowed_tools
  escalation_contract
  prompt_template
```

`escalation_contract` には条件だけを書く。宛先と許可範囲は ParentAuthority からランタイムで
注入する。

### Agent 層

Role を実行する一時的主体である。

```text
Agent:
  model_spec
  system_prompt
  tools
  budget
```

人間も HumanAgent として扱う。人間から AI への置き換えは Agent 差し替えで完結させる。

### Tool 層

具体的な手続きである。Tool は Role と ParentAuthority によって許可される。

```text
Tool examples:
  llm_call
  codex_run
  claude_code_run
  web_crawl
  file_io
  spawn_child
  differentiate
  search_past_subtasks
  request_coordination
  request_escalation
  request_human_review
  terminate_node
  suspend_node
  resume_node
```

Tool は effect type を持つ。

```text
ToolEffect:
  PURE_READ
  LOCAL_WRITE
  EXTERNAL_READ
  EXTERNAL_WRITE
  CONTROL
  HUMAN
```

`EXTERNAL_WRITE` と `CONTROL` は `idempotency_key` を必須とする。idempotency は、同じ要求が
重複実行されても結果が一回分と同じになる性質である。Event sourcing では projection や副作用
処理の idempotency が重要になるため、この制約を Tool 契約に含める。
[Microsoft Azure Architecture Center: Event Sourcing pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)

---

## 4. Node

Node はタスク、組織骨格、再帰的 VSM を単一の抽象で表す。

```text
Node:
  id
  parent_id
  vsm_position

  goal
  input_data
  constraints
  termination_condition
  terminable

  differentiation_level
  predefined_children

  role_spec
  agent_spec
  parent_authority

  child_ids
  artifact_refs
  summary_refs

  status
  output
```

Run ごとの状態は Node 本体ではなく NodeRunState に分離する。

```text
NodeRunState:
  run_id
  node_id
  status
  budget
  cost_consumed
  context_view_ref
  output_ref
```

### Lifecycle

```text
CREATED
  -> RUNNING
  -> IDLE
  -> WAITING_ESCALATION
  -> SUSPENDED
  -> COMPLETED
  -> TERMINATED
  -> FAILED
```

- `COMPLETED` は termination_condition を満たした正常終了である。
- `TERMINATED` は authority による不可逆停止である。
- `FAILED` は回復不能な失敗である。
- `WAITING_ESCALATION` は親または authority Node の判断待ちである。

永続 Node は削除されないが、常時 RUNNING である必要はない。入力待ち、周期待ち、予算待ち、
人間待ちの Node は IDLE または SUSPENDED として休眠できる。

### Node の通称分類

| 通称 | terminable | termination_condition | 生成経路 |
|---|---:|---|---|
| StaticNode | False | None | config |
| 永続 DynamicNode | True | None | spawn |
| 通常 DynamicNode | True | あり | spawn |

`terminable=False` な Node は config 由来に限定する。

---

## 5. u-VSM と分化

すべての Node は u-VSM として扱う。u-VSM はまず `COLLAPSED` 状態で spawn される。そこから
分化を選択した主体 Agent は、その u-VSM の S5 として残り続ける。展開度
(`differentiation_level`)は、S5 以外の System がどこまで実体化しているかを表す。まだ
実体化していない System の責任は、分化主体である S5 Agent が兼ねる。

```text
COLLAPSED:
  spawn 直後の未分化 u-VSM。まだ S5 と他 System の展開を開始していない。

S5_ONLY:
  分化を選択した主体 Agent が S5 となり、VSM 全体を兼ねる。
  S1, S2, S3, S3*, S4 はまだ実体化していない。

PARTIAL:
  S5 以外の一部 System のみ実体化している。
  実体化していない部分は S5 Agent が兼ねる。

FULL:
  S1, S2, S3, S3*, S4, S5 が実体化している。
```

分化は `differentiate` Tool を通じて行い、`ParentAuthority.may_differentiate_to` を超えられ
ない。`differentiate` は分化主体を別の System に置き換える操作ではなく、S5 である主体のもとに
S1 / S2 / S3 / S3* / S4 を実体化していく操作である。分化は `COLLAPSED` → `S5_ONLY` →
`PARTIAL` → `FULL` の一方向に進み、いちど分化した u-VSM が未分化へ戻ることはない。構造変更は
`node_created` / `node_differentiated` / lifecycle event として Event_Log に残り、`LiveTopology`
や graph は Event_Log から再構成される projection として扱う。

---

## 6. Topology

`static_topology` は config 由来の初期構造であり、Run 中に変更しない。

`live_topology` は Event_Log から再構成される実行時構造であり、`spawn_child`、`differentiate`、
`suspend`、`terminate` によって変化する。

```text
static_topology:
  - id: root_s5
    role: S5_POLICY
    terminable: false
    differentiation_level: COLLAPSED
    delegates_to: [s3_main, s4_main]

  - id: s3_main
    role: S3_ALLOCATOR
    parent: root_s5
    delegates_to: [sales_dept]
    fallback: spawn_dynamic

  - id: sales_dept
    role: S1_WORKER
    parent: s3_main
    specialization: 営業
    differentiation_level: COLLAPSED
```

`static_topology` は seed topology であり、実行時の成長は `live_topology` に記録する。

---

## 7. ParentAuthority

ParentAuthority は、親が子に発行する capability として扱う。capability は、ある主体が特定
条件内で特定操作を行えることを示す、期限付き・取消可能な権限である。

```text
ParentAuthority:
  authority_id
  issuer_node_id
  subject_node_id
  issued_at
  expires_at

  may_differentiate_to
  max_depth
  max_spawn_count
  budget_envelope

  allowed_tool_classes
  denied_tool_classes

  data_scope
  secret_scope
  network_scope
  filesystem_scope

  termination_authority
  escalation_contract
```

Role は静的な職務契約を表し、ParentAuthority は動的な制約を表す。

---

## 8. Event_Log

Event_Log は append-only な Source of Truth である。Node Tree、Graph、Markdown home view、
検索 index はすべて projection であり、Event_Log から再生成可能でなければならない。

Event sourcing では append-only event store から現在状態や materialized view を再構成する。
projection は遅延更新されるため eventual consistency を持つ。
[AWS Prescriptive Guidance: Event sourcing pattern](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/event-sourcing.html)

```text
EventEnvelope:
  event_id
  seq
  run_id
  node_id
  stream_id
  stream_version
  event_type
  schema_version
  ts
  actor_type
  actor_id
  correlation_id
  causation_id
  payload
```

`stream_id` は整合性境界を表す。`correlation_id` は一連の業務上の流れを束ねる ID である。
`causation_id` は直接の原因になった event_id である。`schema_version` は payload の構造
バージョンである。

主要イベント型は次の通り。

```text
node_created
node_started
node_idled
node_suspended
node_resumed
node_completed
node_terminated
node_failed
node_differentiated

agent_attached
spec_revised

tool_invoked
tool_completed
tool_failed

budget_consumed
authority_granted
authority_revised

coordination_requested
coordination_decided
escalation_requested
human_review_requested

summary_generated
artifact_created
```

LLM / Codex / Claude Code 呼び出しは Tool invocation として記録する。replay 時には外部 Tool を
再実行せず、`tool_completed` event に保存された result を参照する。Temporal の workflow
determinism に関する文書でも、LLM / AI invocation や外部 API 呼び出しは replay path の外へ置く
べき操作として扱われている。
[Temporal: Workflow Definition determinism and constraints](https://docs.temporal.io/workflow-definition)

---

## 9. Projection と Graph

Projection は Event_Log から再生成可能な派生ビューである。

```text
Projection:
  projection_name
  projection_version
  last_seq
  last_event_id
```

Projection は同じ event_id を二度処理しても結果が変わらない idempotent な更新のみを行う。

成果物と知識は Node Tree とは別の graph として表現する。

```text
Graph node:
  Node
  Artifact
  Concept
  Decision
  Question
  ExternalRef
  TaskSummary

Graph edge:
  PRODUCED_BY
  SPAWNED
  DIFFERENTIATED_INTO
  DECIDED
  SUPERSEDES
  REFERENCES
  DEPENDS_ON
  AUTHORIZED_BY
  REVIEWED_BY
  COORDINATED_BY
```

初期実装は JSONL + SQLite の adjacency list とする。スキーマが安定した後、Kùzu / Neo4j などへ
移行できるようにする。Kùzu は embedded property graph database として Cypher と property graph
model を提供するため、後段の候補になる。[Kùzu documentation](https://kuzudb.github.io/docs/)

---

## 10. 履歴、文脈、検索

Event_Log の生ログは保持する。ただし Agent に渡すのは生ログではなく、Event_Log、TaskSummary、
Artifact、Decision から構成した context view とする。

秘密情報、個人情報、大容量本文は Event_Log に直接埋め込まず、Artifact store または Secret store
への object_ref として記録する。append-only log は削除要求や秘匿要求と衝突しうるため、個人情報を
event store 外に置く設計が推奨される。
[Microsoft Azure Architecture Center: Personal data and regulatory compliance](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)

```text
TaskSummary:
  goal_achieved
  approach
  preconditions
  output_pointer
  dead_ends
  open_questions
  reusability_hints
```

検索の既定範囲は、自分が過去に spawn した直接 child の TaskSummary とする。Agent は必要な場合、
理由を Event_Log に記録したうえで、親 Node、S4 Node、または許可された knowledge index へ検索
範囲を広げられる。広域検索は ParentAuthority の data_scope に従う。

---

## 11. S2 Coordination

S2_Coordinator は Node として存在し、調停履歴と判断履歴を保持する。

他 Node は `request_coordination` Tool を通じて S2_Coordinator に調停を依頼できる。
`request_coordination` Tool は S2 Node への facade であり、調停判断そのものは S2 Node の Agent が
行う。

```text
request_coordination:
  coordination_key
  scope
  participants
  issue
  requested_by
```

`coordination_key` は idempotency key として扱う。同じ `coordination_key` の要求が重複した場合、
S2 は既存の `coordination_decided` を返す。

Node Tree は authority と delegation の縦構造を表す。Sibling Node 間の横方向調停は coordination
graph として表現する。VSM において S2 は operational units 間の衝突や振動を抑える coordination
機能を担うため、この扱いは VSM の役割分担に対応する。
[Viable Systems Model Documentation: Five Subsystems](https://viable-systems.github.io/vsm-docs/overview/what-is-vsm/)

---

## 12. Escalation と Lease

`request_escalation` は Tool として実装する。ただし escalation の条件は
`Role.escalation_contract` に定義し、宛先と許可範囲は ParentAuthority から注入する。

```text
request_escalation:
  escalation_key
  reason
  blocking_issue
  requested_by
  target_authority
```

Node または Tool invocation が外部資源を占有する場合は lease を持つ。

```text
Lease:
  lease_id
  owner_node_id
  resource_ref
  lease_expires_at
```

lease 期限を超えた場合、親 Node、S2、または S3 は recovery policy に従って
release / retry / escalate / terminate を選ぶ。

---

## 13. Spec versioning

RoleSpec、AgentSpec、PromptTemplate は `spec_id` と `spec_version` を持つ。

```text
Spec:
  spec_id
  spec_version
  body
  created_at
```

Node 作成時に参照した Spec は Node に snapshot される。既存 Node は Spec 更新に自動追従しない。
Spec を差し替える場合は `spec_revised` または `agent_attached` event を残す。

---

## 14. Budget

予算は Node Tree に沿って階層的に分配する。

```text
Budget:
  tokens_in
  tokens_out
  tokens_cache_read
  wall_clock_ms
  node_running_ms
```

`[budget]` の Run envelope と `[budget.roles]` のロール別 envelope を
`ParentAuthority.budget_envelope` / `NodeRunState.budget` に注入する。AgentRuntime の応答ごとに
3種のトークン、応答 latency、Node の RUNNING 経過時間を `NodeRunState.cost_consumed` と
`budget_consumed` event に累算する。既消費量が上限以上なら次の AgentRuntime 呼び出しを拒否し、
`budget_exceeded` と `request_escalation` を発行する。

quota 枯渇時は Node を `SUSPENDED` にし、reset 時刻（不明時は設定した間隔）に自動復帰する。
MessageBus は当該 Node の処理中 Message と休眠中に到着した Message を保留し、`quota_resumed`
発行時に同じ購読キューへ再投入する。QuotaMonitor の timer は Platform shutdown で全て cancel する。

---

## 15. Observability

Event_Log は domain event と control event の Source of Truth である。OpenTelemetry は運用観測用
telemetry として使う。

```text
Telemetry correlation:
  event_id
  run_id
  node_id
  tool_invocation_id
  trace_id
  span_id
```

OpenTelemetry は traces、metrics、logs、resources の semantic conventions を定義しており、複数
コンポーネント間の観測データ名を揃えるために使える。
[OpenTelemetry: Semantic Conventions](https://opentelemetry.io/docs/concepts/semantic-conventions/)

Event_Log と telemetry は相互参照できるようにするが、役割は混ぜない。

---

## 16. ディレクトリ構成

```text
vsm/
  architecture/        # Channel, Bus, Event_Log
  roles/               # RoleSpec
  agents/              # Agent 抽象と実装
  tools/               # Tool 実装
  nodes/               # Node, NodeRunState, lifecycle
  authority/           # ParentAuthority, capability
  budget/              # BudgetContext, Ledger
  graph/               # Projection, Artifact/Concept/Decision graph
  memory/              # context view, summary, search policy
  telemetry/           # OpenTelemetry integration
  runtime/             # orchestrator, config loader
```

---

## 17. スコープ外

当面は以下を実装しない。

```text
ハッシュベースのキャッシュ判定
Agent 内部の長期コンテキスト保持
無制限のグローバル過去タスク検索
StaticNode / DynamicNode の別型実装
外部 Tool の replay 時再実行
Event_Log への秘密情報本文の直接保存
```

---

## 18. 未決の設計論点

以下は構造としてまだ確定していない論点である。

```text
無期限 Node の context view 圧縮の発動基準
Event_Log の長期保管、圧縮、暗号化方針
Secret store / Artifact store の具体実装
広域検索を許可する data_scope の粒度
S2 coordination graph の最小スキーマ
lease timeout 後の recovery policy
Run をまたぐ budget accounting の扱い
```

---

## 関連ドキュメント

- [implementation-status.md](implementation-status.md) — 本アーキテクチャの実装到達状況
- [roadmap.md](roadmap.md) — 製品化までの実行計画
- [archive/refactor_20260608.md](archive/refactor_20260608.md) — 本書の元になった作業文書(非公式)
