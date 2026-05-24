# Requirements Document

## Introduction

本フィーチャー `vsm-poc-platform` は、Stafford Beer 提唱の **VSM (Viable System Model)** に基づく「AI 自動会社」の組織アーキテクチャ構想を、**動作する PoC ソフトウェアシステム**として実装するための基盤である。

本 PoC は「**S5 までのサブシステムのタスクをエンドツーエンドで回せる基盤**」を構築することを目的とする。各 System (S1〜S5、S3*) は LLM ベースの AI エージェントとして動作し、VSM の標準チャネルを介してメッセージを交換しながらタスクを処理する。実装言語は Python、LLM 呼び出しは LiteLLM 抽象化層を介し、エージェント連携は外部フレームワークに依存しない自前のメッセージバスで構成する。タスク状態・エージェント記憶・全イベントの永続化は JSONL ファイルを Source of Truth とし、ランタイムでは性能のため Python オブジェクトとしてキャッシュするが、状態の権威は常に JSONL 側に置く。

主要なユースケースはソフトウェア開発タスクの遂行であり、CLI からタスクを投入する。動作確認の代表シナリオは「外部環境からの課題を S4 が同定 → S5 が方針決定 → S5 が S3 へ方針を渡し並行して S4 へ次の調査タスクを依頼 → S3 がリソース配分と S1 群の動的生成 → S2 が S1 群を調整 → S1 群が実行 → S3* が監査」である。

### MVP に含めるもの

- VSM の System 1〜System 5、および S3* 監査機構をソフトウェア構造として表現
- 各 System が LLM ベースの AI エージェントとして動作する仕組み
- VSM 標準チャネル（S1↔S2、S1↔S3、S3↔S4、S3↔S5、S4↔S5、S3* 直接監査）のメッセージング基盤
- CLI からのタスク投入（ファイルパス引数によるファイル内容読み取り対応）
- S1 群の動的生成（上流要件に応じてプール再利用と専門特化の両方を行う）
- 各 System 内に複数のサブエージェントを保持できる構造
- JSONL 構造化ログによる完全な観測性（`vsm status` / `vsm tail` / `vsm replay` を含む）
- 代表シナリオをエンドツーエンドで動作させるデモ

### MVP に含めないもの (明示的にスコープ外)

- FSX (Future-State Expansion) に関する数値最適化・目的関数の実装
- 公共性の測定および勾配的公共性の評価
- 共有剰余の配分ロジック
- 人間の層横断的介入機構 (テンポラル・インターフェース／サブ VSM デプロイ等)
- VSM の動的な内部分化および外部包摂による再帰的成長
- セミステートフル記憶を S2 が集団的に混合する機能
- Web UI ダッシュボード

## Glossary

- **VSM_Platform**: 本 PoC で構築する基盤全体を指すソフトウェアシステム。
- **System**: VSM における S1〜S5 および S3* の総称。本書では特定の System を指す場合は `S1_Worker`, `S2_Coordinator`, `S3_Allocator`, `S3Star_Auditor`, `S4_Scanner`, `S5_Policy` と表記する。
- **S1_Worker**: VSM の System 1。環境と直接やりとりして価値を生む業務単位。複数存在しうる。
- **S2_Coordinator**: VSM の System 2。S1 群の相互干渉を調整するエージェント。
- **S3_Allocator**: VSM の System 3。S1 群へのリソース配分と内部効率最適化を担うエージェント。
- **S3Star_Auditor**: VSM の System 3* (S3 アスタリスク)。S3 の通常チャネルを補完する直接監査エージェント。
- **S4_Scanner**: VSM の System 4。外部環境を探索し機会と脅威を同定するエージェント。営業サブエージェントとリサーチサブエージェントを配下に持つ。**注記:** 厳密な VSM 理論では営業機能は S4 配下に位置付けられないが、本 PoC では S4_Scanner の稼働を試行する目的に限り、便宜的に営業 Sub_Agent を S4_Scanner の配下に配置している。本配置は MVP におけるモック構成であり、後続イテレーションで再配置される可能性がある。
- **S5_Policy**: VSM の System 5。組織のアイデンティティと最上位政策を担当し、S3 と S4 のバランスを取るエージェント。
- **Sub_Agent**: 単一の System の内部に配置される個別の LLM エージェントインスタンス。1 つの System は複数の Sub_Agent を持ちうる。
- **Channel**: 2 つの System 間でメッセージを受け渡すための通信路。VSM の構造的チャネル定義に従う。
- **Message_Bus**: 全 System を接続する自前実装のメッセージング基盤。Channel の集合をホストする。
- **Task**: VSM_Platform に投入される作業単位。ソフトウェア開発タスクを主たる対象とする。
- **Run**: VSM_Platform の 1 回の実行セッション。一意な `run_id` を持ち、独立した JSONL ログ群を保持する。
- **Event_Log**: 1 つの Run における全てのメッセージ送受信、LLM 入出力、状態遷移、エラーを記録する JSONL ファイル群。Source of Truth として機能する。
- **Source_of_Truth**: システム状態の権威的な保管場所。本 PoC では Event_Log がこれに該当する。
- **CLI**: コマンドラインインターフェース。`vsm` 実行ファイルを通じてタスク投入と観測を行う。
- **LLM_Provider_Abstraction**: LiteLLM 等を用いた LLM プロバイダー抽象化層。複数プロバイダーへの差し替えを可能にする。

## Requirements

### Requirement 1: VSM 構造の表現

**User Story:** PoC 利用者として、VSM の S1〜S5 および S3* をソフトウェア上で識別可能な構造として持ちたい。それにより VSM 理論に対応する形でシステム挙動を観察できるようにするためである。

#### Acceptance Criteria

1. THE VSM_Platform SHALL provide distinct software components, each with a unique role identifier and an independently observable lifecycle, for each of S1_Worker, S2_Coordinator, S3_Allocator, S3Star_Auditor, S4_Scanner, and S5_Policy.
2. WHEN a Run is started, THE VSM_Platform SHALL instantiate at least one S2_Coordinator, at least one S3_Allocator, at least one S3Star_Auditor, at least one S4_Scanner, and at least one S5_Policy before transitioning the Run to an active state.
3. THE VSM_Platform SHALL allow between 0 and 1024 S1_Worker instances to exist at startup time.
4. THE VSM_Platform SHALL allow each System to host between 1 and 64 Sub_Agent instances within itself.
5. WHEN a Run is started, THE VSM_Platform SHALL emit, within 5 seconds of Run start, an Event_Log entry containing the identity, role, and configured Sub_Agent count of every instantiated System.
6. WHEN an S1_Worker creation is requested during an active Run and the current S1_Worker count is below 1024, THE VSM_Platform SHALL create and register the new S1_Worker instance and emit an Event_Log entry containing its identity and role within 5 seconds of creation.
7. IF instantiation of any mandatory System (S2_Coordinator, S3_Allocator, S3Star_Auditor, S4_Scanner, or S5_Policy) fails during Run start, THEN THE VSM_Platform SHALL abort the Run, emit an Event_Log entry indicating the failed System role and the failure reason, and transition the Run to a failed state without instantiating additional Systems.

### Requirement 2: VSM 標準チャネルによるメッセージング

**User Story:** PoC 利用者として、VSM の標準チャネル定義に沿った System 間通信を行いたい。それにより VSM 理論上の情報フローを忠実に再現するためである。

#### Acceptance Criteria

1. THE Message_Bus SHALL provide a bidirectional Channel between S1_Worker instances and S2_Coordinator.
2. THE Message_Bus SHALL provide a bidirectional Channel between S1_Worker instances and S3_Allocator.
3. THE Message_Bus SHALL provide a bidirectional Channel between S3_Allocator and S4_Scanner.
4. THE Message_Bus SHALL provide a bidirectional Channel between S3_Allocator and S5_Policy.
5. THE Message_Bus SHALL provide a bidirectional Channel between S4_Scanner and S5_Policy.
6. THE Message_Bus SHALL provide a unidirectional audit Channel from S3Star_Auditor to S1_Worker instances that does not route audit messages through S3_Allocator.
7. IF a System sends a message on a Channel that is not defined in acceptance criteria 1 through 6, THEN THE Message_Bus SHALL reject the message without delivering it to any receiver and SHALL return a rejection indication to the sender that identifies the rejected Channel.
8. IF a message is rejected under acceptance criterion 7, THEN THE Message_Bus SHALL record one rejection event in the Event_Log containing the sender System identifier, the attempted receiver System identifier, the attempted Channel identifier, and a timestamp with millisecond precision.
9. WHEN a message is delivered on a Channel defined in acceptance criteria 1 through 6, THE Message_Bus SHALL append one Event_Log entry containing the sender System identifier, the receiver System identifier, the Channel identifier, the message payload, and a timestamp with millisecond precision.

### Requirement 3: LLM ベースのエージェント実行

**User Story:** PoC 利用者として、各 System が LLM を用いて自律的にタスクを処理できるようにしたい。それにより AI エージェントによる組織運営の挙動を観察するためである。

#### Acceptance Criteria

1. THE VSM_Platform SHALL invoke LLM completions through an LLM_Provider_Abstraction that supports at least one provider at MVP release.
2. WHEN a System receives a message on a Channel defined in Requirement 2 and the message requires dispatch to a Sub_Agent for reasoning, THE System SHALL invoke its assigned Sub_Agent to produce a response via the LLM_Provider_Abstraction.
3. WHEN a Sub_Agent invokes the LLM_Provider_Abstraction, THE VSM_Platform SHALL append an Event_Log entry containing the System identifier, the Sub_Agent identifier, the model name, the prompt, the response, the latency in milliseconds, and the token usage within 1 second of receiving the LLM response.
4. THE VSM_Platform SHALL enforce a per-invocation timeout of 60 seconds on every LLM_Provider_Abstraction call.
5. IF an LLM invocation exceeds the 60 second timeout defined in acceptance criterion 4, THEN THE VSM_Platform SHALL cancel the invocation, append an Event_Log entry of type `llm_timeout` containing the System identifier, the Sub_Agent identifier, and the elapsed milliseconds, and SHALL deliver a typed error message to the calling System within 1 second of cancellation.
6. IF an LLM invocation returns a provider-level error before the timeout, THEN THE VSM_Platform SHALL append an Event_Log entry of type `llm_error` containing the System identifier, the Sub_Agent identifier, the provider error code, and the provider error message, and SHALL deliver a typed error message to the calling System within 1 second of receiving the provider error.
7. THE LLM_Provider_Abstraction SHALL select the underlying LLM provider from either the `LITELLM_PROVIDER` environment variable or a configuration file entry, and SHALL allow swapping the provider through configuration without modification of System or Sub_Agent code.

### Requirement 4: タスク投入インターフェース

**User Story:** PoC 利用者として、CLI からタスクを投入し、ファイル内容を文脈として渡したい。それにより手元のファイルに対してソフトウェア開発タスクを実行させるためである。

#### Acceptance Criteria

1. THE VSM_Platform SHALL expose a CLI command that accepts a Task description of between 1 and 8192 ASCII characters and starts a Run.
2. IF the Task description provided to the CLI is empty or exceeds 8192 ASCII characters, THEN THE VSM_Platform SHALL terminate with a non-zero exit code and SHALL print to standard error a message identifying the violated length constraint.
3. WHERE the CLI is invoked with one or more file path arguments, THE VSM_Platform SHALL read the contents of each referenced file as UTF-8 text and SHALL include the contents as Task context.
4. THE VSM_Platform SHALL accept files of up to 1 MB in size per file path argument.
5. IF a file path argument refers to a path that does not exist, cannot be read, exceeds 1 MB in size, or is not valid UTF-8, THEN THE VSM_Platform SHALL terminate with a non-zero exit code and SHALL print to standard error a message identifying the offending file path and the violated constraint.
6. WHEN a Task is accepted by the CLI, THE VSM_Platform SHALL assign the Task a UUIDv4 Task identifier and the Run a UUIDv4 Run identifier, and SHALL append an Event_Log entry containing the Task identifier, the Run identifier, the Task description, the file path arguments, and the submission timestamp within 1 second of acceptance.
7. WHEN a Task is accepted by the CLI, THE CLI SHALL print the assigned Run identifier and Task identifier to standard output within 5 seconds of acceptance.

### Requirement 5: S4_Scanner による環境同定

**User Story:** PoC 利用者として、S4_Scanner が外部環境からの情報を取得し、機会と脅威を同定する挙動を観察したい。それにより VSM の代表シナリオを起点から動作確認するためである。

#### Acceptance Criteria

1. WHEN a Run is started, THE S4_Scanner SHALL register at least one 営業 Sub_Agent and at least one リサーチ Sub_Agent before any Task is dispatched to S4_Scanner.
2. WHEN a Task is dispatched to S4_Scanner, THE S4_Scanner SHALL produce an environment assessment within 60 seconds of receiving the Task.
3. THE environment assessment produced by S4_Scanner SHALL contain a list of zero or more opportunities and a list of zero or more threats, and each list element SHALL contain an identifier and a description of at least 1 character.
4. WHEN S4_Scanner produces an environment assessment, THE S4_Scanner SHALL deliver the assessment to S5_Policy on the S4-S5 Channel within 5 seconds of production.
5. IF a Sub_Agent of S4_Scanner fails to produce an assessment within 30 seconds of receiving its subtask, THEN THE S4_Scanner SHALL append an Event_Log entry of type `sub_agent_error` containing the Sub_Agent identifier, the elapsed milliseconds, and the failure reason, and SHALL continue producing the environment assessment using the remaining Sub_Agents.
6. IF delivery of an environment assessment to S5_Policy on the S4-S5 Channel fails, THEN THE S4_Scanner SHALL retry delivery up to 3 times with at least 10 seconds between attempts, and SHALL append an Event_Log entry of type `delivery_error` containing the attempt count and the failure reason after each failed attempt.
7. WHEN S5_Policy issues a follow-up investigation request on the S4-S5 Channel, THE S4_Scanner SHALL accept the request and SHALL produce an updated environment assessment within 60 seconds of receipt.

### Requirement 6: S5_Policy による方針決定と並行ディスパッチ

**User Story:** PoC 利用者として、S5_Policy が方針を決定し、S3_Allocator と S4_Scanner の双方に並行して指示を出す挙動を観察したい。それにより VSM における S5 の役割を確認するためである。

#### Acceptance Criteria

1. WHEN S5_Policy receives an environment assessment from S4_Scanner, THE S5_Policy SHALL produce a policy decision that contains directive content for S3_Allocator and a follow-up investigation request for S4_Scanner.
2. WHEN S5_Policy produces a policy decision, THE S5_Policy SHALL dispatch the directive to S3_Allocator on the S3-S5 Channel within 500 milliseconds of policy decision production.
3. WHEN S5_Policy produces a policy decision, THE S5_Policy SHALL dispatch the follow-up investigation request to S4_Scanner on the S4-S5 Channel within 500 milliseconds of policy decision production.
4. THE S5_Policy SHALL complete both dispatches defined in acceptance criteria 2 and 3 within 1 second of policy decision production.
5. IF dispatch to either S3_Allocator or S4_Scanner fails, THEN THE S5_Policy SHALL continue dispatching to the other recipient without blocking, and SHALL append an Event_Log entry of type `dispatch_error` containing the failed recipient identifier, the failed Channel identifier, and the failure reason within 1 second of the failure.
6. WHEN a policy decision is produced, THE S5_Policy SHALL append an Event_Log entry containing the policy decision identifier, the originating environment assessment identifier, the directive content, and the follow-up investigation request content within 1 second of production.

### Requirement 7: S3_Allocator によるリソース配分と S1 群の動的生成

**User Story:** PoC 利用者として、S3_Allocator が方針を受けて S1_Worker 群を動的に生成し、リソースを配分する挙動を観察したい。それにより上流要件に応じた実行体制の組成を確認するためである。

#### Acceptance Criteria

1. WHEN S3_Allocator receives a directive from S5_Policy on the S3-S5 Channel, THE S3_Allocator SHALL determine the required set of S1_Worker instances, including the specialization label and count for each, within 30 seconds of receiving the directive.
2. WHEN S3_Allocator determines the required S1_Worker set under acceptance criterion 1, THE S3_Allocator SHALL prefer reusing an existing idle S1_Worker over instantiating a new S1_Worker for the same specialization, where an idle S1_Worker is defined as an S1_Worker with zero current assignments and a specialization label that matches the requested specialization.
3. WHEN no idle S1_Worker is available for a requested specialization, THE S3_Allocator SHALL instantiate a new S1_Worker with a unique identifier, the requested specialization label, and an initial work assignment within 5 seconds of the determination.
4. WHEN S3_Allocator instantiates a new S1_Worker, THE S3_Allocator SHALL append an Event_Log entry containing the new S1_Worker identifier, the specialization label, and the initial work assignment within 1 second of instantiation.
5. IF instantiation of an S1_Worker fails, THEN THE S3_Allocator SHALL append an Event_Log entry of type `s1_instantiation_error` containing the requested specialization label and the failure reason, and SHALL notify S5_Policy on the S3-S5 Channel of the instantiation failure within 5 seconds of the failure.
6. WHEN S3_Allocator assigns a work item to an S1_Worker, THE S3_Allocator SHALL send the assignment on the S1-S3 Channel within 1 second of assignment.
7. WHEN S3_Allocator sends a work assignment under acceptance criterion 6, THE S3_Allocator SHALL append an Event_Log entry containing the S1_Worker identifier, the work item identifier, and the assignment payload within 1 second of sending.
8. WHEN S3_Allocator receives a completion or failure message from an S1_Worker on the S1-S3 Channel, THE S3_Allocator SHALL forward an internal status report to S5_Policy on the S3-S5 Channel within 5 seconds of receipt.

### Requirement 8: S2_Coordinator による S1 群の調整

**User Story:** PoC 利用者として、複数の S1_Worker が並行実行されるときに S2_Coordinator が干渉を調整する挙動を観察したい。それにより VSM の振動・衝突防止機能を確認するためである。

#### Acceptance Criteria

1. WHILE two or more S1_Worker instances are executing assignments concurrently, THE S2_Coordinator SHALL monitor the S1-S2 Channel for coordination requests and conflict signals.
2. WHEN two or more S1_Worker instances with the same specialization label hold the same work item identifier at the same time, THE S2_Coordinator SHALL recognize this as a conflict.
3. WHEN S2_Coordinator detects a conflict under acceptance criterion 2, THE S2_Coordinator SHALL produce a coordination directive within 5 seconds of detection.
4. WHEN S2_Coordinator produces a coordination directive, THE S2_Coordinator SHALL deliver the directive to every affected S1_Worker on the S1-S2 Channel within 1 second of production.
5. WHEN an S1_Worker receives a coordination directive on the S1-S2 Channel, THE S1_Worker SHALL acknowledge receipt on the S1-S2 Channel and SHALL apply the directive to its subsequent execution within 1 second of receipt.
6. IF S2_Coordinator does not receive an acknowledgement from an affected S1_Worker within 30 seconds of directive delivery, THEN THE S2_Coordinator SHALL append an Event_Log entry of type `coordination_ack_missing` containing the S1_Worker identifier, the directive identifier, and the elapsed milliseconds.
7. THE S2_Coordinator SHALL append an Event_Log entry for every detected conflict, every produced coordination directive, and every received acknowledgement within 1 second of the corresponding event.

### Requirement 9: S3Star_Auditor による直接監査

**User Story:** PoC 利用者として、S3Star_Auditor が S3_Allocator の通常チャネルを介さず S1_Worker を直接監査する挙動を観察したい。それにより VSM の S3* 機能を確認するためである。

#### Acceptance Criteria

1. THE S3Star_Auditor SHALL poll the state of every S1_Worker through the direct audit Channel either at intervals of 30 seconds or upon completion of an S1_Worker assignment, whichever occurs first, without sending observation requests through S3_Allocator.
2. WHEN S3Star_Auditor performs an observation under acceptance criterion 1, THE S3Star_Auditor SHALL append an Event_Log entry of type `audit_observation` containing the S1_Worker identifier, the observed state, and the observation timestamp within 1 second of observation.
3. WHEN an observation triggers production of an audit finding, THE S3Star_Auditor SHALL produce the audit finding within 60 seconds of the triggering observation.
4. WHEN S3Star_Auditor produces an audit finding, THE S3Star_Auditor SHALL append an Event_Log entry of type `audit_finding` containing the audit finding identifier, the originating S1_Worker identifier, and the finding content within 1 second of production.
5. WHEN S3Star_Auditor produces an audit finding, THE S3Star_Auditor SHALL deliver the audit finding to S5_Policy on the audit reporting Channel within 5 seconds of production.
6. WHEN S3Star_Auditor delivers an audit finding to S5_Policy, THE S3Star_Auditor SHALL append an Event_Log entry of type `audit_report_sent` containing the audit finding identifier and the delivery timestamp within 1 second of delivery.

### Requirement 10: JSONL を Source of Truth とする永続化

**User Story:** PoC 利用者として、Run 中の全イベントを JSONL として保存し、後からコーディングエージェントが読み取って状態を再構成できるようにしたい。それにより観測・デバッグ・回帰検証を容易にするためである。

#### Acceptance Criteria

1. THE VSM_Platform SHALL designate the Event_Log as the Source_of_Truth for the state of every Run.
2. THE VSM_Platform SHALL accept Run identifiers consisting of between 1 and 64 ASCII characters.
3. WHEN a Run is started, THE VSM_Platform SHALL create a directory at `runs/{run_id}/` and SHALL create the file `runs/{run_id}/events.jsonl` within that directory before the Run transitions to an active state.
4. IF creation of the directory `runs/{run_id}/` or the file `runs/{run_id}/events.jsonl` fails, THEN THE VSM_Platform SHALL abort the Run, terminate with a non-zero exit code, and SHALL print to standard error a message containing the offending path and the failure reason.
5. WHEN any of the following occurs, THE VSM_Platform SHALL append a single JSON object on a single line to `events.jsonl` within 100 milliseconds of the occurrence: a message is sent on a Channel, a message is received on a Channel, a Sub_Agent invokes the LLM_Provider_Abstraction, a Sub_Agent receives an LLM response, a Task changes state, an S1_Worker is instantiated or terminated, an audit finding is produced, an error is raised.
6. IF an append operation under acceptance criterion 5 fails, THEN THE VSM_Platform SHALL retry the append up to 3 times with at least 100 milliseconds between attempts before surfacing the failure to the calling System through a typed error message.
7. THE VSM_Platform SHALL include in every appended JSON object a UTC timestamp in ISO 8601 format with millisecond precision, an event type identifier, and a Run identifier.
8. THE VSM_Platform SHALL preserve the order of appended events such that, for any two events appended during the same Run, the event appended first appears on an earlier line in `events.jsonl` than the event appended second (FIFO ordering).
9. THE VSM_Platform SHALL allow runtime caching of state in Python objects but SHALL treat the Event_Log as authoritative such that any cached state can be reconstructed by replaying the Event_Log.
10. FOR ALL events appended to the Event_Log during a Run, replaying the Event_Log SHALL produce a reconstructed state in which the set of Tasks and their states, the lifecycle history of every S1_Worker, the sequence of Channel events, and the set of audit findings each match the runtime cached state at the time of replay element-by-element (round-trip property).

### Requirement 11: CLI 観測コマンド群

**User Story:** PoC 利用者として、Run 中および Run 完了後に CLI から状態とイベントを観測したい。それにより別途 Web UI を用意せずにデバッグを完結させるためである。

#### Acceptance Criteria

1. THE CLI SHALL provide a `vsm status` subcommand that reads the Event_Log of a specified Run identifier and SHALL print to standard output, within 5 seconds of subcommand invocation, the current set of Tasks as `(Task_id, Task_state)` tuples followed by the current set of System instances as `(System_id, Sub_Agent_count)` tuples, with one tuple per line.
2. THE CLI SHALL provide a `vsm tail` subcommand that follows a specified Run's Event_Log and SHALL print every appended event to standard output within 1 second of the event being appended to `events.jsonl`.
3. THE `vsm tail` subcommand SHALL accept zero or more `--system <name>` options and zero or more `--channel <name>` options, where multiple values for the same option SHALL be combined with logical OR within that option, and the two options SHALL be combined with logical AND across options.
4. WHERE neither `--system` nor `--channel` is specified, THE `vsm tail` subcommand SHALL apply no filter and SHALL print every appended event.
5. THE CLI SHALL provide a `vsm replay` subcommand that reads a Run's Event_Log and SHALL print to standard output one line per event, in append order, where each line contains the timestamp, the System identifier, the Channel identifier, and the event type identifier separated by a single space character.
6. WHEN the `vsm replay` subcommand is invoked against a Run that is still active, THE CLI SHALL print to standard error a warning message identifying the Run as active before printing the snapshot of currently appended events to standard output.
7. IF any CLI observation subcommand is invoked against a Run identifier for which no Event_Log file exists, THEN THE CLI SHALL print to standard error the message `Event_Log not found for run <id>` with the offending Run identifier substituted, and SHALL terminate with a non-zero exit code.

### Requirement 12: エンドツーエンドの代表シナリオ

**User Story:** PoC 利用者として、合意された代表シナリオが S1〜S5 と S3* を貫通して動作することを確認したい。それにより本 PoC が「S5 までのサブシステムのタスクを回せる基盤」であることを検証するためである。

#### Acceptance Criteria

1. WHEN the demonstration scenario is executed, THE CLI SHALL acknowledge acceptance of the initial Task and print the Run identifier and Task identifier to standard output within 5 seconds of invocation.
2. WHEN the demonstration scenario is executed, THE S4_Scanner SHALL produce at least one environment assessment and SHALL deliver the assessment to S5_Policy on the S4-S5 Channel within 300 seconds of Task acceptance.
3. WHEN the demonstration scenario is executed, THE S5_Policy SHALL produce at least one policy decision and SHALL dispatch the directive to S3_Allocator on the S3-S5 Channel and the follow-up investigation request to S4_Scanner on the S4-S5 Channel within 600 seconds of Task acceptance.
4. WHEN the demonstration scenario is executed, THE S3_Allocator SHALL instantiate at least one S1_Worker, SHALL assign at least one work item via the S1-S3 Channel, and SHALL receive at least one completion message from an S1_Worker via the S1-S3 Channel.
5. WHEN the demonstration scenario is executed, THE S2_Coordinator SHALL receive at least one signal on the S1-S2 Channel and the receipt SHALL be recorded in the Event_Log.
6. WHEN the demonstration scenario is executed, THE S3Star_Auditor SHALL produce at least one audit finding and SHALL deliver the finding to S5_Policy on the audit reporting Channel within 600 seconds of Task acceptance.
7. WHEN the demonstration scenario completes, THE Event_Log SHALL contain at least one event for each of S1_Worker, S2_Coordinator, S3_Allocator, S3Star_Auditor, S4_Scanner, and S5_Policy.
8. THE demonstration scenario SHALL be considered complete when the Event_Log contains at least one event attributed to each of S1_Worker, S2_Coordinator, S3_Allocator, S3Star_Auditor, S4_Scanner, and S5_Policy and at least one completion event from an S1_Worker.
9. IF the demonstration scenario does not satisfy acceptance criterion 8 within 1800 seconds of Task acceptance, THEN THE VSM_Platform SHALL terminate the Run with a non-zero exit code and SHALL print to standard error a message identifying the missing System events.

### Requirement 13: 構造制約の保証

**User Story:** PoC 利用者として、ユーザーが明示した構造制約 (S2〜S5 は最低 1 つ必須、各 System 内に複数 Sub_Agent を保持可能、S1 は複数存在しうる) がランタイムでも保証されるようにしたい。それにより VSM 構造の最低条件を逸脱した状態で Run が始まることを防ぐためである。

#### Acceptance Criteria

1. WHEN a Run start is requested, THE VSM_Platform SHALL verify, before any S1_Worker is created and before any task is dispatched to any System, that at least one configured instance exists for each of S2_Coordinator, S3_Allocator, S3Star_Auditor, S4_Scanner, and S5_Policy.
2. IF the verification in acceptance criterion 1 detects one or more missing required Systems, THEN THE VSM_Platform SHALL abort startup without dispatching any task and SHALL terminate the process with a non-zero exit code.
3. IF the verification in acceptance criterion 1 detects one or more missing required Systems, THEN THE VSM_Platform SHALL write to standard error an error message that names every missing System among S2_Coordinator, S3_Allocator, S3Star_Auditor, S4_Scanner, and S5_Policy.
4. THE VSM_Platform SHALL allow each of S2_Coordinator, S3_Allocator, S3Star_Auditor, S4_Scanner, and S5_Policy to be configured with between 1 and 16 Sub_Agent instances at Run start time.
5. THE VSM_Platform SHALL allow the S1_Worker instance count to be zero at Run start time.
6. WHILE a Run is in progress, THE VSM_Platform SHALL allow S3_Allocator to dynamically create additional S1_Worker instances up to a configured maximum of 64 concurrent instances.

### Requirement 14: スコープ外機能の非導入保証

**User Story:** PoC 利用者として、合意された MVP スコープ外の機能が誤って実装されないようにしたい。それにより PoC が当初の目的に集中し、後続イテレーションのための明確な境界を保つためである。

#### Acceptance Criteria

1. THE VSM_Platform SHALL NOT expose, at MVP release, any executable code path, API endpoint, or user-invocable command that performs numerical optimization or evaluates objective functions related to FSX.
2. THE VSM_Platform SHALL NOT expose, at MVP release, any executable code path, API endpoint, or user-invocable command that performs publicness measurement or gradient publicness evaluation.
3. THE VSM_Platform SHALL NOT expose, at MVP release, any executable code path, API endpoint, or user-invocable command that performs shared surplus allocation.
4. THE VSM_Platform SHALL NOT expose, at MVP release, any executable code path, API endpoint, or user-invocable command that implements cross-layer human intervention mechanisms such as temporal interfaces or sub-VSM deployment.
5. THE VSM_Platform SHALL NOT expose, at MVP release, any executable code path, API endpoint, or user-invocable command that performs dynamic internal differentiation or external subsumption for recursive growth.
6. THE VSM_Platform SHALL NOT expose, at MVP release, any executable code path, API endpoint, or user-invocable command that performs collective semi-stateful memory mixing within S2_Coordinator.
7. THE VSM_Platform SHALL NOT expose, at MVP release, any web user interface dashboard reachable over HTTP or HTTPS.
8. IF the CLI receives a request that maps to a capability listed in acceptance criteria 1 through 7, THEN THE VSM_Platform SHALL terminate with a non-zero exit code and SHALL print to standard error the message `requested capability is out of MVP scope` followed by the name of the requested capability.
9. THE VSM_Platform SHALL document the boundaries enumerated in acceptance criteria 1 through 7 in the project README under a section titled `MVP Scope Boundaries`.
