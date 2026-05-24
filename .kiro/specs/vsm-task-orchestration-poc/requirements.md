# Requirements Document

## Introduction

本ドキュメントは、VSM（Viable System Model / 実行可能システムモデル）に基づくAI自動会社の組織アーキテクチャ構想のPoC（Proof of Concept）として動作するソフトウェア基盤の要件を定義する。

PoCのゴールは、VSMの5つのサブシステム（S1: 実行、S2: 調整、S3: 内部最適化、S3*: 監査、S4: 環境モニタリング、S5: 政策）をAIエージェントとしてそれぞれ実装し、システム間の規定チャネルを通じて「S5の政策発行 → S4の環境取り込み → S3の資源配分 → S2のS1間調整 → S1の実タスク処理 → S3*の監査」というVSMの基本的な情報フロー／タスクフローが一貫して成立することを実証することである。

MVPでは、各エージェントは原則としてステートレスなLLM呼び出しを基本とし、必要に応じて外部メモリ／ログによるセミステートフル運用を許容する。FSX（Future-State Expansion）に基づく数値最適化、勾配的公共性指標、共有剰余の配分ルール、人間の横断的タスク回収機能、再帰的サブVSM増殖、非序列的参加の評価・報酬設計はMVPの対象外とするが、後続拡張で差し込めるよう、システム間チャネルとエージェント実装の差し替えを意識した拡張点（スタブ）を設けることを前提とする。

## Glossary

- **VSM_System**: 本PoC全体のソフトウェア基盤。S1〜S5の各サブシステム、システム間チャネル、タスクオーケストレータを含む。
- **Subsystem**: VSM_Systemを構成する論理的単位。S1_Agent、S2_Agent、S3_Agent、S3_Star_Agent、S4_Agent、S5_Agentの総称。
- **S1_Agent**: VSMのSystem 1に対応するAIエージェント。実タスクを実行する責務を持つ。複数インスタンスが並列に存在しうる。
- **S2_Agent**: VSMのSystem 2に対応するAIエージェント。複数のS1_Agent間の干渉・競合を調整する責務を持つ。
- **S3_Agent**: VSMのSystem 3に対応するAIエージェント。S1_Agent群への資源配分および内部最適化を担う。
- **S3_Star_Agent**: VSMのSystem 3*に対応するAIエージェント。S1_Agentの実行結果に対する監査を担う。
- **S4_Agent**: VSMのSystem 4に対応するAIエージェント。VSM_System外部の環境シグナルを取り込み、内部に通知する責務を持つ。
- **S5_Agent**: VSMのSystem 5に対応するAIエージェント。VSM_System全体の政策（Policy）を保持・発行する責務を持つ。
- **Policy**: S5_Agentが発行する、VSM_System全体の方針を表す構造化データ。
- **Environmental_Signal**: S4_Agentが外部から取り込む構造化データ。市場・業務環境などを抽象化した入力。
- **Resource_Allocation**: S3_Agentが各S1_Agentに対して発行する、タスク配分および資源（処理予算など）配分を表す構造化データ。
- **Coordination_Directive**: S2_AgentがS1_Agent間の干渉を調整するために発行する指示を表す構造化データ。
- **Task**: S1_Agentが処理する作業単位を表す構造化データ。識別子、内容、入力ペイロードを含む。
- **Task_Result**: S1_AgentがTaskを処理した結果を表す構造化データ。識別子、ステータス、出力ペイロードを含む。
- **Audit_Report**: S3_Star_Agentが発行する監査結果を表す構造化データ。
- **Channel**: 2つのSubsystem間でメッセージを送受信するための通信抽象。送信元、送信先、メッセージ種別、ペイロードを持つ。
- **Orchestrator**: VSM_System内でSubsystem群とChannel群を起動・接続し、タスクフローを駆動する制御コンポーネント。
- **LLM_Backend**: AIエージェントが利用する大規模言語モデル呼び出しの抽象インタフェース。
- **Run_Log**: VSM_Systemが1回のエンドツーエンド実行で生成する、Channel上の全メッセージとSubsystem出力の時系列記録。
- **Extension_Point**: 後続拡張（人間介入、FSX目的関数、可逆委任、再帰的サブVSM）のためにMVPで定義される拡張用インタフェース。MVPではスタブ実装を持つ。

## Requirements

### Requirement 1: VSMサブシステムの構成

**User Story:** PoC運用者として、VSMの5つのサブシステムが独立したコンポーネントとして起動できる基盤が欲しい。これにより、VSMの組織骨格をソフトウェアとして表現できる。

#### Acceptance Criteria

1. THE VSM_System SHALL S1_Agent、S2_Agent、S3_Agent、S3_Star_Agent、S4_Agent、S5_Agentの6種類のSubsystemを起動する。
2. THE VSM_System SHALL 1つ以上のS1_Agentインスタンスを並列に起動する機能を提供する。
3. THE VSM_System SHALL S2_Agent、S3_Agent、S3_Star_Agent、S4_Agent、S5_Agentをそれぞれ単一インスタンスとして起動する。
4. THE VSM_System SHALL 各Subsystemの種別と識別子をRun_Logに記録する。

### Requirement 2: S5_Agentによる政策発行

**User Story:** PoC運用者として、S5_AgentがVSM_System全体の方針となるPolicyを発行することを確認したい。これにより、上位方針が下位サブシステムに伝播する起点が成立する。

#### Acceptance Criteria

1. WHEN VSM_Systemが起動する、THE S5_Agent SHALL Policyを1件発行する。
2. THE S5_Agent SHALL 発行したPolicyを政策チャネル経由でS3_AgentおよびS4_Agentに送信する。
3. THE S5_Agent SHALL 発行するPolicyに識別子、発行時刻、方針本文を含める。
4. WHEN S5_Agentが新たなPolicyを発行する、THE S5_Agent SHALL 直前のPolicyを置き換える。
5. IF S5_AgentがLLM_Backendからの応答取得に失敗する、THEN THE S5_Agent SHALL Run_Logにエラーを記録し、既定のフォールバックPolicyを発行する。

### Requirement 3: S4_Agentによる環境モニタリング

**User Story:** PoC運用者として、S4_Agentが外部のEnvironmental_Signalを取り込み、内部に通知することを確認したい。これにより、VSM_Systemが外部環境の変化を反映できる。

#### Acceptance Criteria

1. THE S4_Agent SHALL 環境シグナル入力ソースから0件以上のEnvironmental_Signalを取得する。
2. WHEN Environmental_Signalを取得する、THE S4_Agent SHALL 取得したEnvironmental_Signalを環境チャネル経由でS3_AgentおよびS5_Agentに送信する。
3. THE S4_Agent SHALL 現行のPolicyを政策チャネルから受信した上で、Environmental_Signalの解釈を行う。
4. IF 環境シグナル入力ソースが利用不可能である、THEN THE S4_Agent SHALL 空のEnvironmental_Signal集合を出力し、Run_Logに利用不可状態を記録する。

### Requirement 4: S3_Agentによる資源配分

**User Story:** PoC運用者として、S3_Agentが現行PolicyとEnvironmental_Signalに基づいてS1_Agent群へResource_Allocationを発行することを確認したい。これにより、内部最適化機能が成立する。

#### Acceptance Criteria

1. WHEN S3_AgentがPolicyとEnvironmental_Signalの両方を受信した、THE S3_Agent SHALL 起動中の各S1_Agentに対して1件ずつResource_Allocationを発行する。
2. THE S3_Agent SHALL 発行するResource_Allocationに、対象S1_Agent識別子、割り当てるTask集合、処理予算を含める。
3. THE S3_Agent SHALL 発行したResource_Allocationを資源配分チャネル経由で各S1_Agentに送信する。
4. WHEN S3_AgentがS3_Star_AgentからAudit_Reportを受信した、THE S3_Agent SHALL 直後のResource_Allocation判断にAudit_Reportの内容を反映する。
5. IF 起動中のS1_Agentが0件である、THEN THE S3_Agent SHALL Resource_Allocationを発行せず、Run_Logに対象S1非存在状態を記録する。

### Requirement 5: S2_AgentによるS1間調整

**User Story:** PoC運用者として、S2_AgentがS1_Agent間の干渉を検出してCoordination_Directiveを発行することを確認したい。これにより、横断的な調整機能が成立する。

#### Acceptance Criteria

1. THE S2_Agent SHALL 各S1_Agentから現在処理中Task情報を調整チャネル経由で受信する。
2. WHEN 2つ以上のS1_Agentが同一資源識別子を含むTaskを処理しようとする、THE S2_Agent SHALL 該当S1_Agentに対してCoordination_Directiveを発行する。
3. THE S2_Agent SHALL 発行するCoordination_Directiveに、調整対象のS1_Agent識別子集合と実行順序指示を含める。
4. THE S2_Agent SHALL 発行したCoordination_Directiveを調整チャネル経由で対象S1_Agentに送信する。
5. WHERE S1_Agent間に干渉が検出されない場合、THE S2_Agent SHALL Coordination_Directiveを発行しない。

### Requirement 6: S1_Agentによるタスク実行

**User Story:** PoC運用者として、S1_AgentがS3_Agentから受信したResource_Allocation内のTaskを実行し、Task_Resultを生成することを確認したい。これにより、最下位の実行機能が成立する。

#### Acceptance Criteria

1. WHEN S1_AgentがResource_Allocationを受信した、THE S1_Agent SHALL Resource_Allocation内の各Taskに対してTask_Resultを生成する。
2. THE S1_Agent SHALL 生成したTask_ResultをS3_Star_Agentに監査チャネル経由で送信する。
3. WHEN S1_AgentがS2_AgentからCoordination_Directiveを受信した、THE S1_Agent SHALL Coordination_Directive内の実行順序指示に従ってTaskを処理する。
4. THE S1_Agent SHALL 自身が現在処理中のTaskに含まれる資源識別子をS2_Agentに調整チャネル経由で通知する。
5. IF S1_AgentがTaskの処理に失敗する、THEN THE S1_Agent SHALL 失敗ステータスのTask_Resultを生成し、エラー内容をTask_Resultのペイロードに含める。

### Requirement 7: S3_Star_Agentによる監査

**User Story:** PoC運用者として、S3_Star_AgentがS1_Agentの実行結果を監査し、結果をS3_AgentおよびS5_Agentに通知することを確認したい。これにより、独立監査機能が成立する。

#### Acceptance Criteria

1. WHEN S3_Star_AgentがTask_Resultを受信した、THE S3_Star_Agent SHALL Task_Resultに対してAudit_Reportを生成する。
2. THE S3_Star_Agent SHALL 発行するAudit_Reportに、対象Task識別子、対象S1_Agent識別子、判定結果、判定理由を含める。
3. THE S3_Star_Agent SHALL 発行したAudit_ReportをS3_AgentおよびS5_Agentに監査結果チャネル経由で送信する。
4. THE S3_Star_Agent SHALL S3_Agentが発行するResource_Allocationと独立した経路でTask_Resultを取得する。

### Requirement 8: システム間チャネル

**User Story:** PoC運用者として、サブシステム間のメッセージ送受信が定義されたチャネル経由で行われることを確認したい。これにより、各システムの責務分離と後続のチャネル差し替えが成立する。

#### Acceptance Criteria

1. THE VSM_System SHALL 政策チャネル、環境チャネル、資源配分チャネル、調整チャネル、監査チャネル、監査結果チャネルの6種類のChannelを提供する。
2. THE VSM_System SHALL 各Channelに対して送信元Subsystem種別と送信先Subsystem種別を定義する。
3. THE VSM_System SHALL 各Channelで送受信されるメッセージ種別をスキーマとして規定する。
4. WHEN Subsystemが規定外のChannelを通じてメッセージを送受信しようとする、THE VSM_System SHALL 当該送受信を拒否し、Run_Logに違反を記録する。
5. THE VSM_System SHALL 各Channelを通過する全メッセージをRun_Logに記録する。

### Requirement 9: エンドツーエンドタスクフロー

**User Story:** PoC運用者として、S5の政策発行から始まり、S4・S3・S2・S1・S3*を経由してタスク処理と監査が完了する一連のフローが1回の実行で成立することを確認したい。これにより、PoCの「動作するMVP」が達成される。

#### Acceptance Criteria

1. WHEN VSM_Systemに1回のオーケストレーションサイクル開始指示を受信した、THE Orchestrator SHALL S5発行 → S4取り込み → S3配分 → S2調整 → S1実行 → S3*監査の順序で各Subsystemを起動する。
2. WHEN 1サイクル内で全Subsystemの処理が完了した、THE Orchestrator SHALL サイクル完了状態をRun_Logに記録する。
3. THE VSM_System SHALL 1サイクル内で発行された各メッセージに、サイクル識別子を付与する。
4. WHEN Orchestratorがサイクルを完了した、THE Orchestrator SHALL 当該サイクルで生成された全Task_Resultと全Audit_Reportを集約した実行サマリを出力する。
5. IF 1サイクル内のいずれかのSubsystemがエラーで停止した、THEN THE Orchestrator SHALL 残余Subsystemをスキップし、停止位置と原因をRun_Logに記録する。

### Requirement 10: AIエージェント抽象化

**User Story:** PoC運用者として、各Subsystemが共通のAIエージェント抽象を通じてLLM_Backendを呼び出すことを確認したい。これにより、エージェント実装の差し替えが成立する。

#### Acceptance Criteria

1. THE VSM_System SHALL 各SubsystemがLLM_Backendへのアクセスに使用する共通インタフェースを提供する。
2. THE VSM_System SHALL Subsystemをステートレスモード（毎回独立したLLM呼び出し）またはセミステートフルモード（外部メモリを伴うLLM呼び出し）のいずれかで起動する設定機能を提供する。
3. WHERE Subsystemがセミステートフルモードで起動された、THE Subsystem SHALL 外部メモリストアに対して読み出しと書き込みを行う。
4. THE VSM_System SHALL Subsystem実装をLLM_Backend呼び出しを行わないモック実装に差し替える機能を提供する。

### Requirement 11: 後続拡張のためのExtension_Point

**User Story:** PoC運用者として、人間介入チャネル、FSX目的関数、可逆委任、再帰的サブVSMなど後続拡張のための差し込み点がMVPに用意されていることを確認したい。これにより、MVP拡張時に基盤の作り直しを回避できる。

#### Acceptance Criteria

1. THE VSM_System SHALL 人間介入用Extension_Pointを各Subsystem入出力経路に提供する。
2. THE VSM_System SHALL 目的関数評価用Extension_PointをS3_Agentの判断経路に提供する。
3. THE VSM_System SHALL 可逆委任用Extension_PointをS5_AgentおよびS3_Agentの判断経路に提供する。
4. THE VSM_System SHALL 再帰的サブVSM用Extension_PointをS1_Agentの内部に提供する。
5. WHERE Extension_Pointがスタブ実装の状態である、THE Extension_Point SHALL 入力をそのまま透過させる動作を行う。
6. THE VSM_System SHALL 各Extension_Pointの差し込み有無と差し込み実装識別子をRun_Logに記録する。

### Requirement 12: 観測性

**User Story:** PoC運用者として、1回のサイクル実行で発生した全メッセージとSubsystem出力を時系列で確認したい。これにより、VSMフローが意図通りに動作したかを検証できる。

#### Acceptance Criteria

1. THE VSM_System SHALL 各Subsystem出力、Channel通過メッセージ、Orchestratorの状態遷移をRun_Logに時系列で記録する。
2. THE Run_Log SHALL 各エントリにタイムスタンプ、サイクル識別子、Subsystem識別子、メッセージ種別を含める。
3. THE VSM_System SHALL Run_Logを構造化フォーマット（JSON Lines形式）で永続化する。
4. THE VSM_System SHALL 1サイクル完了時に、当該サイクル分のRun_Logエントリを取得するインタフェースを提供する。

### Requirement 13: エラー処理

**User Story:** PoC運用者として、Subsystemやチャネルで発生したエラーが他のSubsystemに波及せずに記録されることを確認したい。これにより、PoC実行中の障害分析が成立する。

#### Acceptance Criteria

1. IF SubsystemがLLM_Backendから5xx応答もしくはタイムアウトを受信する、THEN THE Subsystem SHALL Run_Logにエラー種別を記録し、既定回数まで再試行する。
2. IF Subsystemが既定再試行回数を超えてもLLM_Backend応答取得に失敗する、THEN THE Subsystem SHALL 失敗ステータスを伴う既定の出力を生成する。
3. IF メッセージスキーマ検証に失敗する、THEN THE VSM_System SHALL 当該メッセージを破棄し、Run_Logにスキーマ違反を記録する。
4. WHEN OrchestratorがSubsystem起動例外を検出した、THE Orchestrator SHALL 例外内容をRun_Logに記録し、サイクルを安全に停止する。

### Requirement 14: パーサーとシリアライザ

**User Story:** PoC開発者として、Channel間で送受信される構造化メッセージ（Policy、Environmental_Signal、Resource_Allocation、Coordination_Directive、Task、Task_Result、Audit_Report）を信頼できる方法で文字列表現と相互変換したい。これにより、ログ永続化、LLMプロンプト埋め込み、メッセージ復元が一貫して成立する。

#### Acceptance Criteria

1. THE VSM_System SHALL Policy、Environmental_Signal、Resource_Allocation、Coordination_Directive、Task、Task_Result、Audit_Reportの各メッセージ種別に対するシリアライザを提供する。
2. THE VSM_System SHALL 各メッセージ種別に対するパーサーを提供する。
3. WHEN パーサーが妥当な文字列表現を入力として受け取った、THE パーサー SHALL 対応するメッセージオブジェクトを返却する。
4. WHEN パーサーが妥当でない文字列表現を入力として受け取った、THE パーサー SHALL 構造化エラーを返却する。
5. THE VSM_System SHALL 任意のメッセージオブジェクトに対して、シリアライザ適用後にパーサーを適用すると元と等価なメッセージオブジェクトが得られる（ラウンドトリップ性）ことを保証する。

### Requirement 15: 非機能要件

**User Story:** PoC運用者として、PoC基盤が現実的な時間で1サイクル実行を完了し、コンポーネント差し替えが容易であることを確認したい。これにより、MVPの実証と後続拡張が両立する。

#### Acceptance Criteria

1. WHEN モック実装のSubsystemのみで1サイクル実行する、THE Orchestrator SHALL 5秒以内にサイクル完了状態に到達する。
2. WHEN 実LLM_Backendを使用する1サイクル実行を行う、THE Orchestrator SHALL 120秒以内にサイクル完了状態または所定の失敗状態に到達する。
3. THE VSM_System SHALL 各Subsystem実装、Channel実装、LLM_Backend実装を独立したモジュール単位で差し替え可能とする。
4. THE VSM_System SHALL 起動時設定ファイル経由でSubsystem構成（インスタンス数、モード、Extension_Point差し込み）を指定する機能を提供する。
