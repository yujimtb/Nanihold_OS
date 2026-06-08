# Nanihold_OS リファクタリング基礎文書（案）

## 0. この文書の位置づけ

VSM PoC として動作する現行実装を、実運用可能なアーキテクチャへ再構築するための設計指針。実装手順ではなく、構造の取り決めを定める。

## 1. 設計原則

**Architecture / Role / Agent / Tool を分離する。** VSMの構造（誰が誰に伝えるか）、役割の契約（何のために存在するか）、実行主体（どうやって応答するか）、実行手段（具体的な手続き）を、それぞれ独立した層として扱う。

**履歴はタスクに帰属し、Agentはステートレスとする。** Agent内部にコンテキストを持たない。すべての文脈はタスクノードに残る。これによりAgentの差し替え・再開・モデル変更が一貫して可能になる。

**判断は局所化する。** 再利用判定、分解判断、エスカレーション判断などは、それを行う主体（エージェント）の直近の文脈だけで完結する形に設計する。グローバルな整合性に依存しない。

**永続性は「終了条件のないタスク」として表現する。** PersistentAgentのような特別な構造を持たない。Agentは常にephemeralで、役割の継続性はタスク側の終了条件で表現する。

## 2. 四層構造

### Architecture層

VSMの骨格。System、Channel、Message、Event_Log、再帰構造。LLMやCodexの存在を知らない。「組織図」のみを表現する。

### Role層

各Systemに紐づく契約定義。データ（YAML/JSON）として保持する。

```
Role:
  id, vsm_position
  responsibility           # 自然言語の責任記述
  input_schema, output_schema
  allowed_tools            # 使用許可されたツール
  escalation_targets       # 困った時の上位
  prompt_template          # システムプロンプトの雛形
```

Role単体ではメッセージに応答できない。実体化にはAgentが必要。

### Agent層

Role仕様を実行する一時的な主体。タスクノードに紐づいて生成され、タスク完了で破棄される。

```
Agent:
  model_spec               # codex / claude_code / llm(model_id) / human(id)
  system_prompt            # Role.prompt_templateから生成
  tools                    # Role.allowed_toolsから注入
  budget                   # タスクから割り当てられた予算
```

人間もAgentの一種（HumanAgent）として第一級に扱う。AIへの段階的置き換えはAgent差し替えだけで済む。Agentはステートレスで、起動時にタスクノードのコンテキストを読み、終了時に書き戻す。

### Tool層

実行手段の集合。`llm_call`, `codex_run`, `claude_code_run`, `web_crawl`, `gmail_send`, `file_io`, `spawn_subtask`, `search_past_subtasks`, `request_human_review`, `terminate_task` など。各ToolはExecutorから呼ばれる純粋な手続きで、許可制御はRoleとS5_Policyが担う。

## 3. タスク木（Task Tree）

### 構造

タスクは木構造を成す有向グラフ。S5を起点にS4, S3が分解し、S1（あるいはspawnされた下位System）の末端で実行される。各ノードは独立した実行単位であり、自分のコンテキスト、Agent定義、子ノード参照、終了状態を持つ。

### TaskNodeの最小構成

```
TaskNode:
  id, parent_id, vsm_position
  
  # 不変の入力
  goal, input_data, constraints
  termination_condition     # 有限 or 無限（永続タスク）
  
  # 実行定義
  role_spec, agent_spec, budget
  
  # 実行履歴（追記のみ）
  context_window            # Agentとのやり取り
  tool_invocations          # ツール呼び出し記録
  child_node_ids
  
  # 完了情報
  status                    # pending / running / completed / failed / terminated
  output
  summary                   # 完了時に生成（再利用判定の材料）
  cost_consumed
```

### 永続タスク

終了条件を持たないタスク。S5_Policy（会社方針の維持）、S1_Sales内部の継続的目標（顧客を増やす）など、継続的役割はこれで表現される。Agentは応答ごとに生成・破棄されてよく、コンテキストはノードに残る。再起動時は新しいAgentが同じノードを引き継ぐ。

上位からの明示的な`terminate`命令で終了する。コンテキストの肥大化に対しては、要約圧縮を子タスクとしてspawnする方式で対処する。

## 4. 履歴と再利用判定

### 要約による再利用

ハッシュベースの同一性判定は採用しない。文面の揺れに弱く、実用にならない。

各タスクは完了時に、それを実行したAgent自身が**TaskSummary**を生成する。

```
TaskSummary:
  goal_achieved             # 何を達成したか
  approach                  # 採った分解戦略
  preconditions             # 依存した前提
  output_pointer            # 出力本体への参照（要約には含めない）
  dead_ends                 # 試したが失敗した方向
  open_questions            # 残した不確実性
  reusability_hints         # 自己申告された再利用可能性
```

### 検索範囲は「自分が過去に委譲した子」のみ

エージェントは新しいタスクを委譲しようとする時、Toolを通じて**自分の直接の子の履歴**だけを参照する。

```
search_past_subtasks(query, filter) -> list[TaskSummary]
```

この制約により、判断の局所性と再利用判定の局所性が一致する。各エージェントは自分の判断粒度で再利用可否を判断し、下位の細かい判断は下位エージェントに委ねる。コンテキスト爆発と検索ノイズを構造的に回避する。

### 4つの判断選択肢

Agentは過去要約を見た上で次のいずれかを選ぶ。選択理由はEvent_Logに残す。

- **reuse**: 過去ノードの出力をそのまま採用
- **resume**: 過去コンテキストを引き継いで追加指示で再実行
- **branch_with_reference**: 過去を参照情報として渡しつつ新規実行
- **fresh**: 過去を参照せず新規実行

## 5. ツール

### spawn_subtask

子タスクノードを生成するTool。呼び出すAgentが、子のRole仕様・AgentSpec（モデルとプロンプト）・予算・終了条件を定義する。再帰的にVSMをネストできる。

S5_Policyが、spawnの深さ・総数・親予算からの分配率に制限をかける。当面は静的構成主体で運用し、動的spawnは制限を強くかけて運用する。

### コストと予算の階層化

予算はタスク木に沿って階層的に分配される。親が子をspawnする時、自分の予算から子に分配する。Tool呼び出しのたびに当該ノードの予算から減算され、超過時にToolが拒否する。

コストの記録粒度：トークン、壁時計時間、外部API単価、人間Agentの拘束時間。すべてEvent_Logに `tool_invoked` / `tool_completed` で残す。

### 主要なTool一覧

- 実行系: `llm_call`, `codex_run`, `claude_code_run`
- 構造系: `spawn_subtask`, `terminate_task`, `search_past_subtasks`
- 監査系: `request_human_review`
- 外部系: `web_crawl`, `gmail_send`, `file_io`

各ToolはRoleの`allowed_tools`で許可制御される。Tool呼び出しはすべてEvent_Logに残り、監査対象となる。

## 6. Event_Log と Task Tree の関係

Event_Logは**出来事の時系列**、Task Treeは**構造の永続表現**。両者はSource of Truthの一貫性を保つ：Event_LogからTask Treeは再構成可能であり、Task Treeへの変更は必ずEvent_Logに対応イベントを残す。

追加すべきイベント型：
`task_node_created`, `task_node_started`, `task_node_completed`, `task_node_failed`, `task_decomposed`, `task_reused_from_cache`, `task_subtree_invalidated`, `agent_assigned`, `tool_invoked`, `tool_completed`, `budget_consumed`, `human_review_requested`, `human_review_provided`, `summary_generated`.

## 7. 人間とAIの境界

人間はHumanAgentとしてAgent抽象に統一する。S3*_Auditorのような役割を、当面はHumanAgent、将来はLLM/CodexベースのAgentで担当する。Roleもタスク構造も変えずにAgent差し替えだけで移行できる。

人間判断はEvent_Logに構造化データで残す（判定種別、理由タグ、修正指示、コメント）。これが将来の自動化Agent訓練データになる。

## 8. ディレクトリ構成（目標形）

```
vsm/
  architecture/        # System, Channel, Bus, Event_Log
  roles/               # Role定義（YAML）と Role抽象
  agents/              # Agent抽象と実装（llm/codex/claude_code/human）
  tools/               # Tool実装
  tasks/               # TaskNode, TaskTree, 永続化, 検索
  budget/              # BudgetContext, Ledger
  runtime/             # Platform orchestrator
```

## 9. スコープから外すもの

ハッシュベースのキャッシュ判定。永続Agentの専用クラス。Agentが自前でコンテキストを保持する設計。グローバルな過去タスク検索。これらは本リファクタリングの方針として採用しない。

## 10. 未決の論点

- `search_past_subtasks` の検索範囲が Run 内に閉じるか、永続タスクと共に Run を超えるか
- 並列実行された兄弟タスクの相互可視性（基本不可視、S2_Coordinator経由のみ可能とするか）
- 永続タスクのコンテキスト圧縮の発動基準
- タスク木のルート（最上位の永続タスク）を誰が生成するか
- S2_Coordinator の横方向調停を Task Tree 上でどう表現するか（Toolとして実装する案）
