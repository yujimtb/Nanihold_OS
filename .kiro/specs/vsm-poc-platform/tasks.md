# Implementation Plan: vsm-poc-platform

## Overview

本実装計画は `vsm-poc-platform` を **Python 3.11+ / asyncio 単一プロセス** で TDD 順 (テスト先行 → 実装) に構築する。基盤レイヤ (errors / ids / clock / config) → ミドル (Message_Bus / Event_Log / LLM Provider) → System 群 → CLI → 統合シナリオの順に依存を解決し、design.md §Correctness Properties の P1〜P17 を Hypothesis ベース PBT として 1 property = 1 sub-task で実装する。各 PBT には `@settings(max_examples=100)` を付与し、各実装タスクには対応する Requirement と Property への参照を明記する。

LLM 呼び出しは原則 `FakeLLMProvider` で差し替え、実 LLM は代表シナリオの 1 ケースのみ `@pytest.mark.live_llm` でゲートする。代表シナリオは `tests/integration/test_representative_scenario.py` で 3 ケース (success / timeout / replay-roundtrip) を扱う。

## Tasks

- [x] 1. Project scaffolding をセットアップする
  - `pyproject.toml` を作成 (Python 3.11+, dependencies: `litellm`, `hypothesis`, `pytest`, `pytest-asyncio`, `typer`, `pydantic>=2`)
  - `vsm/` パッケージのディレクトリ構造を design.md §パッケージレイアウト に従って作成 (`vsm/messaging/`, `vsm/eventlog/`, `vsm/llm/`, `vsm/systems/`, `vsm/runtime/`, `tests/unit/`, `tests/property/`, `tests/integration/`)
  - 各サブパッケージに空の `__init__.py` を配置
  - `.gitignore` を作成 (`runs/`, `__pycache__/`, `.pytest_cache/`, `.hypothesis/`, `.venv/` を含む)
  - `README.md` の雛形を作成し `MVP Scope Boundaries` セクションのプレースホルダを置く (Task 33 で本文を書く, REQ 14.9)
  - `pytest.ini` または `pyproject.toml [tool.pytest.ini_options]` で `asyncio_mode = "auto"` と marker `live_llm` を登録
  - `vsm` コンソールスクリプトエントリポイントを `pyproject.toml` に登録 (`vsm = "vsm.cli:app"`)
  - _Requirements: 3.1, 4.1, 14.9_

- [x] 2. Foundation primitives: errors / ids / clock を実装する
  - [x] 2.1 `vsm/errors.py` に例外階層を実装する
    - `VSMError`, `ConfigError`, `CLIError(exit_code)`, `RunDirectoryError`, `MessagingError`, `ChannelRejected`, `LLMError`, `LLMTimeoutError`, `LLMProviderError`, `EventLogError`, `EventLogAppendError`, `SystemInstantiationError`, `DispatchError`, `SubAgentError`, `CoordinationAckMissing` を定義
    - design.md §Error Handling §例外階層 と Exit Code 体系に厳密一致させる
    - _Requirements: 1.7, 2.7, 3.5, 3.6, 4.2, 4.5, 5.5, 6.5, 7.5, 8.6, 10.4, 10.6, 11.7, 13.2, 14.8_
  - [x]* 2.2 `tests/unit/test_errors.py` に例外型のユニットテストを書く
    - 各例外が `VSMError` を継承していること、`CLIError.exit_code` が代入可能であることを検証
    - _Requirements: 1.7, 4.2, 14.8_
  - [x] 2.3 `vsm/ids.py` に UUIDv4 生成と `run_id` バリデータを実装する
    - `generate_run_id() -> str` (UUIDv4 prefix `run-` を含む 1〜64 ASCII 文字)
    - `validate_run_id(s: str) -> None` (1〜64 ASCII 範囲外 / 非 ASCII / 空文字を `CLIError(exit_code=2)` で拒否)
    - `generate_uuid() -> str` (UUIDv4 hex)
    - _Requirements: 4.6, 10.2, 11.7_
  - [x]* 2.4 `tests/property/test_ids.py` に `run_id` 境界の PBT を書く
    - **Property 13 (一部): CLI input validation の run_id 部分**
    - **Validates: Requirements 10.2, 11.7**
    - Hypothesis で長さ 0 / 1 / 64 / 65 と非 ASCII を生成し validate の挙動を確認
    - `@settings(max_examples=100)`
  - [x] 2.5 `vsm/clock.py` に UTC clock 抽象を実装する
    - `Clock` プロトコル (`now() -> datetime`, `now_iso() -> str`, `monotonic() -> float`)
    - `SystemClock` (本番), `FakeClock` (テスト, タイムスキップ可能)
    - `now_iso` は ISO 8601 + millisecond precision (UTC `Z` 終端)
    - _Requirements: 2.8, 2.9, 10.5, 10.7_
  - [x]* 2.6 `tests/unit/test_clock.py` で `now_iso` の ms 精度フォーマットを検証する
    - 正規表現 `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$` にマッチすること
    - _Requirements: 10.7_

- [x] 3. Configuration ローダを実装する
  - [x] 3.1 `vsm/config.py` に `LLMConfig`, `RunConfig`, `vsm.toml` パーサを実装する
    - `LLMConfig.resolve_model()` は環境変数 `LITELLM_PROVIDER` > `vsm.toml` の `llm.provider` の優先順 (REQ 3.7)
    - `RunConfig` は各 System の Sub_Agent 数を保持 (`systems_for(role) -> int`, `count(role) -> int`)
    - 範囲外値 (Sub_Agent 数 < 1 or > 16, S1 上限 > 1024) は `ConfigError` で拒否
    - _Requirements: 1.3, 1.4, 3.7, 13.4, 13.5, 13.6_
  - [x]* 3.2 `tests/unit/test_config.py` で env var 優先順と境界値を検証する
    - `LITELLM_PROVIDER=openai` 設定時に `resolve_model` が openai 系モデルを返すこと
    - Sub_Agent 数 0, 17 が `ConfigError` を出すこと
    - _Requirements: 1.4, 3.7, 13.4_

- [x] 4. Event_Log のスキーマを定義する
  - [x] 4.1 `vsm/eventlog/schema.py` に共通 Envelope と payload スキーマを実装する
    - pydantic `Event` モデル (`ts: str`, `run_id: str`, `event_type: str`, `seq: int`, `payload: dict`)
    - design.md §Data Models §Event スキーマの 26 個の `event_type` を `Literal` として列挙
    - 各 `event_type` ごとの payload pydantic モデル (system_instantiated, channel_message, channel_rejected, llm_invocation, llm_timeout, llm_error, s4_assessment_produced, sub_agent_error, delivery_error, policy_decision, dispatch_error, s1_instantiated, s1_instantiation_error, s1_assignment_sent, s1_completion, coordination_conflict, coordination_directive, coordination_ack, coordination_ack_missing, audit_observation, audit_finding, audit_report_sent, event_log_append_error, system_instantiation_failed, task_submitted, task_state_changed)
    - _Requirements: 10.7, 10.5_
  - [x]* 4.2 `tests/property/test_event_log_schema.py` で全イベントが必須キーを持つか PBT 検証する
    - **Property 7: Required field presence**
    - **Validates: Requirements 10.7, 10.2**
    - Hypothesis で各 `event_type` の payload を生成 → JSON dump → parse → 必須キー (`ts`, `run_id`, `event_type`, `seq`, `payload`) と `ts` の ms 精度を検証
    - `@settings(max_examples=100)`

- [x] 5. Event_Log Writer を実装する
  - [x] 5.1 `vsm/eventlog/writer.py` に `EventLogWriter` を実装する
    - 単一 writer タスクで `asyncio.Queue` から取り出して `events.jsonl` に append → `flush()` → `os.fsync()`
    - `seq` を 0 起点で writer 側が単調増加で付与
    - `_write_with_retry`: 最大 3 回、各間に `asyncio.sleep(0.1)` (REQ 10.6)、3 回失敗で `EventLogAppendError`
    - `append(event_type, payload) -> awaitable[None]` は 100 ms 以内に完了するパス (REQ 10.5)
    - `runs/{run_id}/events.jsonl` を line-buffered で `"a"` モードオープン、UTF-8 + `ensure_ascii=False`
    - _Requirements: 10.3, 10.5, 10.6, 10.7, 10.8_
  - [x]* 5.2 `tests/property/test_event_log_fifo.py` で FIFO 順序と seq 単調性を PBT 検証する
    - **Property 6: FIFO append order**
    - **Validates: Requirements 10.8**
    - Hypothesis で N 個 (1〜200) の append 操作列を生成し、`asyncio.gather` で並行 enqueue した後 `events.jsonl` を読み戻して `seq` が `0, 1, ..., N-1` で enqueue 順と一致することを検証
    - `@settings(max_examples=100)`
  - [x]* 5.3 `tests/property/test_retry_semantics.py` の Event_Log append 部分を実装する
    - **Property 16 (前半): Retry semantics for Event_Log append**
    - **Validates: Requirements 10.6**
    - Hypothesis で transient failure 列を生成 (k 回連続失敗 → 成功 / 3 回失敗) → 試行回数 ≤ 3 と最小間隔 100 ms と最終 surface 動作を検証
    - 単調時計で間隔をアサート
    - `@settings(max_examples=100)`

- [x] 6. Event_Log Replay を実装する
  - [x] 6.1 `vsm/eventlog/replay.py` と `vsm/runtime/state.py` を実装する
    - `vsm/runtime/state.py`: `ReconstructedState` dataclass (`tasks: dict[str, TaskState]`, `s1_lifecycle: dict[str, list]`, `channel_events: list`, `audit_findings: dict[str, AuditFinding]`)
    - `vsm/eventlog/replay.py`: `replay(path: Path) -> ReconstructedState`、各 `event_type` に対応する `apply` ハンドラを定義
    - apply 順は seq 昇順
    - _Requirements: 10.1, 10.9, 10.10_
  - [x]* 6.2 `tests/property/test_event_log_replay.py` で round-trip を PBT 検証する
    - **Property 5: Event_Log round-trip**
    - **Validates: Requirements 10.1, 10.9, 10.10**
    - Hypothesis で操作列 (task 状態遷移 / S1 instantiate / channel_message / audit_finding) を生成 → ランタイムキャッシュと replay 結果が 4 projection で要素単位一致することを検証
    - `@settings(max_examples=100)`
  - [x] 6.3 `vsm/eventlog/reader.py` に `tail` 用 read-only リーダを実装する
    - `iter_appended(path)` (poll 周期 200 ms で readline)、フィルタ predicate を引数に取る
    - パスが存在しない場合は `CLIError(exit_code=2)` (REQ 11.7)
    - _Requirements: 11.2, 11.7_

- [x] 7. Checkpoint - 基盤レイヤのテストを通す
  - 2.x, 3.x, 4.x, 5.x, 6.x の全テストが green であることを確認する。問題が発生したらユーザに確認する。

- [x] 8. Message_Bus を実装する
  - [x] 8.1 `vsm/messaging/channels.py` に `ChannelId` enum と `ALLOWED_ROUTES` を実装する
    - `ChannelId`: `S1_S2`, `S1_S3`, `S3_S4`, `S3_S5`, `S4_S5`, `S3STAR_TO_S1`, `S3STAR_S5_AUDIT`
    - `SystemRole` enum: `S1_WORKER`, `S2_COORDINATOR`, `S3_ALLOCATOR`, `S3STAR_AUDITOR`, `S4_SCANNER`, `S5_POLICY`
    - `ALLOWED_ROUTES: frozenset[tuple[SystemRole, SystemRole, ChannelId]]` を design.md と同一の 12 ルート (S3* 関連は単方向) で定義
    - `is_allowed(sender_role, receiver_role, channel) -> bool`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 9.1, 9.5_
  - [x] 8.2 `vsm/messaging/message.py` に `Message` dataclass と `SendResult` を実装する
    - `Message(message_id, sender_role, sender_id, receiver_role, receiver_id, channel, payload, timestamp_ms)` (`frozen=True`)
    - `SendResult` は `delivered: bool`, `rejected_channel: ChannelId | None` を持ち、ChannelRejected を非例外として返す形
    - _Requirements: 2.7, 2.8, 2.9_
  - [x] 8.3 `vsm/messaging/bus.py` に `MessageBus` を実装する
    - `subscribe(system_id, channel) -> asyncio.Queue[Message]` で system 起動時に bind
    - `send(msg) -> SendResult`: `is_allowed` チェック → 不許可なら `channel_rejected` を Event_Log に append + `SendResult(delivered=False)` (REQ 2.7, 2.8) → 許可なら `Queue.put_nowait` + `channel_message` を append (REQ 2.9)
    - 同一 event loop tick 内で配信完了 (1 秒 SLA を構造的に満たす)
    - `S3STAR_TO_S1` は S3_Allocator のキューに絶対届かないことをコード構造で保証 (subscribe マップで分離)
    - _Requirements: 2.1〜2.9, 9.1_
  - [x]* 8.4 `tests/property/test_message_bus.py` でチャネル不変条件を PBT 検証する
    - **Property 1: Channel rejection invariant**
    - **Validates: Requirements 2.7, 2.8**
    - **Property 2: Channel delivery invariant**
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.9**
    - Hypothesis で `(sender_role, receiver_role, channel, payload)` を生成し、ALLOWED_ROUTES のメンバ / 非メンバで挙動が分かれることを検証
    - 受信キュー残数と Event_Log エントリの一意性を確認
    - `@settings(max_examples=100)`

- [x] 9. LLM Provider Abstraction を実装する
  - [x] 9.1 `vsm/llm/types.py` と `vsm/llm/provider.py` を実装する
    - `LLMRequest`, `LLMResponse(text, tokens_in, tokens_out, latency_ms)`
    - `LLMProvider.invoke(prompt, model=None) -> LLMResponse` は `litellm.acompletion` を呼び出し、`litellm.exceptions.APIError` を `LLMProviderError` に変換
    - 内部の `timeout=60` と呼び出し側 `asyncio.wait_for(60)` の二重防衛
    - `LITELLM_PROVIDER` 環境変数 / config の差し替えで System コードに変更不要 (REQ 3.7)
    - _Requirements: 3.1, 3.4, 3.6, 3.7_
  - [x] 9.2 `vsm/llm/fake.py` に `FakeLLMProvider` を実装する
    - 応答テキスト (固定 / pattern / callable) / レイテンシ (`asyncio.sleep` 制御) / エラー注入 (timeout, provider error code) を制御可能
    - 全 PBT および統合テストで使用するモック (design.md §Testing Strategy §LLM モック戦略)
    - _Requirements: 3.4, 3.5, 3.6_
  - [x]* 9.3 `tests/unit/test_llm_provider.py` で 60 秒タイムアウトとエラー変換を検証する
    - `FakeLLMProvider(latency=70)` を `asyncio.wait_for(invoke(), 60)` で呼び `LLMTimeoutError` が 1 秒以内に伝達することを確認 (REQ 3.5)
    - `FakeLLMProvider(error=APIError(...))` で `LLMProviderError` への変換を確認 (REQ 3.6)
    - _Requirements: 3.4, 3.5, 3.6_

- [x] 10. System / Sub_Agent 基底クラスを実装する
  - [x] 10.1 `vsm/systems/base.py` に `SubAgent` と `System` 基底クラスを実装する
    - `SubAgent.respond(prompt, context)`: `asyncio.wait_for(self._llm.invoke(...), 60)` で保護し、成功時に `llm_invocation` を 1 秒以内に append (REQ 3.3)、TimeoutError 時に `llm_timeout` を append し `LLMTimeoutError` を raise (REQ 3.5)、`LLMProviderError` で `llm_error` を append し再 raise (REQ 3.6)
    - `System.run()` は abstract、`shutdown()` で配下 task を `cancel()`
    - `register_sub_agent(label)` で 1〜64 範囲を強制 (REQ 1.4)、超過時 `ConfigError`
    - _Requirements: 1.1, 1.4, 3.2, 3.3, 3.4, 3.5, 3.6_
  - [x]* 10.2 `tests/unit/test_subagent.py` で 60 秒タイムアウトと Event_Log 順序を検証する
    - `FakeLLMProvider(latency=0.05)` で正常応答 → `llm_invocation` が 1 秒以内に append されること
    - `FakeLLMProvider(latency=70)` で `llm_timeout` が append され `LLMTimeoutError` が caller に届くこと
    - _Requirements: 3.3, 3.4, 3.5_

- [x] 11. Runtime 構造検証 / Lifecycle を実装する
  - [x] 11.1 `vsm/runtime/lifecycle.py` に `start_run`, `Platform` を実装する
    - 構造検証: 必須 5 役割 (S2/S3/S3*/S4/S5) の count >= 1 を `instantiate_systems` 前に確認 (REQ 13.1)
    - 不足時は `system_instantiation_failed` event を不足 role 毎に append し、stderr に `missing required systems: ...` を書き、exit code 3 で abort (REQ 1.7, 13.2, 13.3)
    - `runs/{run_id}/` 作成失敗時は exit code 4 でメッセージ + `RunDirectoryError` (REQ 10.4)
    - 全必須 System の `system_instantiated` を 5 秒以内に append (REQ 1.5)
    - S1 動的生成上限 64 / 全体 1024 のガード (REQ 13.6, 1.3)
    - `runs/{run_id}/RUNNING` lockfile を作成 (replay の active 判定用, REQ 11.6)
    - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.6, 1.7, 10.3, 10.4, 13.1, 13.2, 13.3, 13.4, 13.5, 13.6_
  - [x]* 11.2 `tests/property/test_lifecycle_verification.py` で構造検証を PBT 検証する
    - **Property 10: Mandatory systems verification**
    - **Validates: Requirements 1.7, 13.1, 13.2, 13.3**
    - Hypothesis で `RunConfig` を生成 (各 role の count を 0〜3 でランダム) → missing が空集合 ⇔ 検証通過、非空 ⇒ exit code 3 + stderr に全 missing role 名を含むことを検証
    - `@settings(max_examples=100)`
  - [x]* 11.3 `tests/property/test_bounded_counts.py` で個数上限を PBT 検証する
    - **Property 11: Bounded counts**
    - **Validates: Requirements 1.3, 1.4, 13.4, 13.5, 13.6**
    - Hypothesis で Sub_Agent 数 (0〜70) と S1 動的生成要求列 (0〜1100) を生成し境界 (16/64/1024) で拒否されること、`|S1Pool|` が常に範囲内に留まることを検証
    - `@settings(max_examples=100)`

- [x] 12. S5_Policy を実装する
  - [x] 12.1 `vsm/systems/s5_policy.py` に `S5_Policy` を実装する
    - environment assessment 受信 → LLM Sub_Agent で `PolicyDecision(directive, followup_request)` 生成
    - `dispatch_decision`: `asyncio.gather(send(directive, S3_S5), send(followup, S4_S5), return_exceptions=True)` で並行送信 (REQ 6.2, 6.3, 6.4)
    - 例外時 `dispatch_error` を 1 秒以内に append (REQ 6.5)、片側失敗が他方をブロックしない
    - `policy_decision` を 1 秒以内に append (REQ 6.6)
    - audit finding 受信ハンドラを実装 (REQ 9.5 受領側)
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 9.5_
  - [x]* 12.2 `tests/property/test_s5_dispatch_resilience.py` で並行ディスパッチを PBT 検証する
    - **Property 15: Concurrent dispatch resilience**
    - **Validates: Requirements 6.4, 6.5**
    - Hypothesis で failure 注入集合 `F ⊆ {S3, S4}` を生成し、`|F|` 個の `dispatch_error` が 1 秒以内、成功側は両方とも 1 秒以内に配送、片側失敗が他方をブロックしないことを検証
    - `@settings(max_examples=100)`
  - [x]* 12.3 `tests/property/test_operation_sla.py` の S5 部分を実装する
    - **Property 4 (S5 部分): Latency-bounded operation invariant**
    - **Validates: Requirements 6.2, 6.3, 6.4**
    - Hypothesis で `PolicyDecision` を生成し、`FakeClock` で directive/followup 個別 500 ms と両方完了 1 秒の SLA を計測
    - `@settings(max_examples=100)`

- [x] 13. S4_Scanner を実装する
  - [x] 13.1 `vsm/systems/s4_scanner.py` に `S4_Scanner` を実装する
    - Run start 前に `営業`, `リサーチ` Sub_Agent を必須登録 (REQ 5.1)
    - 受信タスクから 60 秒以内に `EnvironmentAssessment` を生成 (REQ 5.2)、各 item の description >= 1 char (REQ 5.3)
    - assessment を S5 へ S4_S5 で 5 秒以内に配送、`s4_assessment_produced` を append (REQ 5.4)
    - Sub_Agent 個別 30 秒タイムアウト → `sub_agent_error` append + 残り Sub_Agent で継続 (REQ 5.5)
    - 配送失敗時 3 回まで 10 秒間隔リトライ、各失敗で `delivery_error` を append (REQ 5.6)
    - S5 からの follow-up 受信時に 60 秒以内に updated assessment (REQ 5.7)
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_
  - [x]* 13.2 `tests/property/test_retry_semantics.py` の S4 配送リトライ部分を実装する
    - **Property 16 (後半): Retry semantics for S4→S5 delivery**
    - **Validates: Requirements 5.6**
    - Hypothesis で transient failure 列を生成し、最大 3 試行 / 最小 10 秒間隔 / 失敗ごとに `delivery_error` event 生成 / 任意の attempt 成功で停止することを検証
    - `FakeClock` で 10 秒間隔をアサート
    - `@settings(max_examples=100)`
  - [x]* 13.3 `tests/property/test_operation_sla.py` の S4 部分を追記する
    - **Property 4 (S4 部分): Latency-bounded operation invariant**
    - **Validates: Requirements 5.2, 5.5, 5.7**
    - assessment 生成 60 s, sub_agent fallback 30 s, follow-up 60 s を `FakeLLMProvider` でレイテンシ制御し検証
    - `@settings(max_examples=100)`

- [x] 14. S3_Allocator を実装する
  - [x] 14.1 `vsm/systems/s3_allocator.py` に `S3_Allocator` と `S1Pool` を実装する
    - directive 受信 → LLM Sub_Agent で `{specialization: count}` を導出、30 秒以内に決定 (REQ 7.1)
    - `S1Pool.find_idle(spec)`: `len(current_assignments) == 0 and specialization == spec` の最初の S1 を返す (REQ 7.2 idle 定義)
    - idle 不在かつ `|pool| < 64` で新規 S1 instantiation を 5 秒以内 (REQ 7.3, 13.6)、即座に initial assignment を `S1_S3` 経由で送信
    - `s1_instantiated` を 1 秒以内に append (REQ 7.4)
    - instantiation 失敗時 `s1_instantiation_error` append + 5 秒以内に S5 通知 (REQ 7.5)
    - assignment 送信を 1 秒以内 (REQ 7.6) + `s1_assignment_sent` 1 秒以内 (REQ 7.7)
    - S1 完了 / 失敗受信 → 5 秒以内に S5 へ status report (REQ 7.8)
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 13.6_
  - [x]* 14.2 `tests/property/test_s3_allocator.py` で reuse vs instantiate dichotomy を PBT 検証する
    - **Property 8: S1 reuse vs instantiate dichotomy**
    - **Validates: Requirements 7.2, 7.3, 13.6**
    - Hypothesis で `S1Pool` 状態と spec request を生成 → idle 非空時は instantiation event なし & idle のいずれかが選ばれる、idle 空 & |pool|<64 時はちょうど 1 個 `s1_instantiated` event が出ることを検証
    - `@settings(max_examples=100)`

- [x] 15. S2_Coordinator を実装する
  - [x] 15.1 `vsm/systems/s2_coordinator.py` に `S2_Coordinator` と `detect_conflict` を実装する
    - `detect_conflict(s1_states) -> list[Conflict]`: design.md の擬似コードに準拠
    - conflict 検出から 5 秒以内に `CoordinationDirective` 生成 (REQ 8.3)、1 秒以内に全該当 S1 へ `S1_S2` で配信 (REQ 8.4)
    - 30 秒で ack 不達検出 → `coordination_ack_missing` append (REQ 8.6)
    - conflict / directive / ack イベントを各 1 秒以内に append (REQ 8.7)
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.6, 8.7_
  - [x]* 15.2 `tests/property/test_s2_conflict_detection.py` で `detect_conflict` の正確性を PBT 検証する
    - **Property 9: Conflict detection correctness**
    - **Validates: Requirements 8.2**
    - Hypothesis で `S1State` 集合を生成し、仕様の `expected(S)` と `detect_conflict(S)` の `(specialization, work_item_id)` 射影が集合一致することを検証
    - `s1_ids` がすべての該当 S1 を含むこと
    - `@settings(max_examples=100)`

- [x] 16. S3Star_Auditor を実装する
  - [x] 16.1 `vsm/systems/s3star_auditor.py` に `S3Star_Auditor` を実装する
    - 観測スケジューラ: `asyncio.wait({Timer(30s), completion_signal})` で `FIRST_COMPLETED` (REQ 9.1)
    - 観測要求は `S3STAR_TO_S1` を通り S3_Allocator を経由しない (Bus 構造で保証)
    - 各観測ごとに `audit_observation` を 1 秒以内に append (REQ 9.2)
    - 観測トリガで `AuditFinding` を 60 秒以内に生成 (REQ 9.3)、`audit_finding` を 1 秒以内に append (REQ 9.4)
    - finding を `S3STAR_S5_AUDIT` で 5 秒以内に S5 へ配送 (REQ 9.5)、`audit_report_sent` を 1 秒以内に append (REQ 9.6)
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_
  - [x]* 16.2 `tests/property/test_audit_schedule.py` で観測スケジュールを PBT 検証する
    - **Property 14: Audit schedule**
    - **Validates: Requirements 9.1**
    - Hypothesis で completion event 列と Run start を生成し、最後の観測からの経過が常に 30 秒以下、completion から 1 秒以内に観測が発火することを `FakeClock` で検証
    - `@settings(max_examples=100)`

- [x] 17. S1_Worker を実装する
  - [x] 17.1 `vsm/systems/s1_worker.py` に `S1_Worker` を実装する
    - `S1_S3` で assignment 受信 → `current_assignments` に追加 → Sub_Agent (LLM) で実行 → 完了時 `S1_S3` で `s1_completion` 報告
    - `S1_S2` 受信時 1 秒以内に ack を返送し subsequent 実行に directive を反映 (REQ 8.5)
    - `S3STAR_TO_S1` 受信時に現状態を返送 (REQ 9.1 受領側)
    - terminate 時に Event_Log に lifecycle event (terminate) を append
    - _Requirements: 1.6, 7.7, 7.8, 8.5, 9.1_
  - [x]* 17.2 `tests/property/test_event_sla.py` を実装する
    - **Property 3: Event SLA conformance**
    - **Validates: Requirements 1.5, 1.6, 2.9, 3.3, 4.6, 5.4, 6.5, 6.6, 7.4, 7.7, 8.7, 9.2, 9.4, 9.6, 10.5**
    - design.md §Property 3 の SLA テーブル全 13 行をテーブル駆動で検証 (`FakeClock` で計測)
    - 各 occurrence を発火させ、対応 event_type の append が SLA 内に行われたことを確認
    - `@settings(max_examples=100)`

- [x] 18. Checkpoint - System 群とプロパティテストを通す
  - 8.x〜17.x の全テストが green であることを確認する。SLA 違反 / 非決定性が出たら `FakeClock` の差し替えを見直す。問題が発生したらユーザに確認する。

- [x] 19. CLI: `vsm submit` を実装する
  - [x] 19.1 `vsm/cli.py` に Typer アプリと `submit` サブコマンドを実装する
    - description: 1〜8192 ASCII chars (REQ 4.1, 4.2)、違反時 stderr + exit code 2
    - file 引数: 存在 / 1 MB 以下 / UTF-8 のいずれか違反で stderr + exit code 2 (REQ 4.5)
    - 受理時に UUIDv4 task_id / run_id を割当 (REQ 4.6) → `task_submitted` を 1 秒以内に append → `run_id` / `task_id` を 5 秒以内に stdout (REQ 4.7)
    - `submit` は `Platform.submit()` を `asyncio.run` で呼ぶ
    - スコープ外サブコマンド名 (`fsx`, `publicness`, `shared-surplus`, `human-intervention`, `recursive-growth`, `semi-stateful-mix`, `web-ui`) は登録しない (REQ 14.1〜14.7) かつ要求されたら exit code 5 + `requested capability is out of MVP scope <name>` (REQ 14.8)
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 14.8_
  - [x]* 19.2 `tests/property/test_cli_input_validation.py` を実装する
    - **Property 13: CLI input validation**
    - **Validates: Requirements 4.2, 4.5, 10.2, 11.7, 14.8**
    - Hypothesis で description 長 (0, 1, 8192, 8193) / 非 ASCII / 不正ファイル (不存在 / 1MB+1 / 非 UTF-8) を生成し、Run ディレクトリ非作成 / Event_Log エントリ無し / 非ゼロ exit code を検証
    - `@settings(max_examples=100)`
  - [x]* 19.3 `tests/property/test_out_of_scope.py` を実装する
    - **Property 17: Out-of-scope absence and rejection**
    - **Validates: Requirements 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 14.8**
    - Typer アプリの `registered_commands` を introspect して `OUT_OF_SCOPE_NAMES` と交わらないこと、case-insensitive にスコープ外名を要求した時の exit code 5 + stderr message を検証
    - `@settings(max_examples=100)`

- [x] 20. CLI: `vsm status` を実装する
  - [x] 20.1 `vsm/cli.py` に `status` サブコマンドを追加する
    - `runs/{run_id}/events.jsonl` を replay → 5 秒以内に `(task_id, task_state)` 行群 → `(system_id, sub_agent_count)` 行群を stdout (REQ 11.1)
    - run_id バリデーション (REQ 10.2) と Event_Log 不在時の `Event_Log not found for run <id>` + exit code 2 (REQ 11.7)
    - _Requirements: 10.2, 11.1, 11.7_
  - [x]* 20.2 `tests/unit/test_cli_status.py` で status 出力フォーマットを検証する
    - 既知の events.jsonl fixture で出力行が `(task_id, state)` と `(system_id, count)` のタプル形式であることを確認
    - 5 秒 SLA を `FakeClock` で確認
    - _Requirements: 11.1_

- [x] 21. CLI: `vsm tail` を実装する
  - [x] 21.1 `vsm/cli.py` に `tail` サブコマンドを追加する
    - `--system` (複数可、OR) と `--channel` (複数可、OR)、両者は AND (REQ 11.3)
    - 既存内容を読み切ってから `vsm/eventlog/reader.iter_appended` で追従、append から 1 秒以内に出力 (REQ 11.2)
    - フィルタ未指定時は全 event を出力 (REQ 11.4)
    - Event_Log 不在時 stderr + exit code 2 (REQ 11.7)
    - _Requirements: 10.2, 11.2, 11.3, 11.4, 11.7_
  - [x]* 21.2 `tests/property/test_cli_tail_filter.py` を実装する
    - **Property 12: Tail filter semantics**
    - **Validates: Requirements 11.2, 11.3, 11.4**
    - Hypothesis で event 列とフィルタ集合を生成し、`predicate(e) := (Sys==∅ ∨ system_name(e)∈Sys) ∧ (Ch==∅ ∨ channel_name(e)∈Ch)` に厳密一致する部分列が同順で出力されること、`FakeClock` で 1 秒 SLA を検証
    - `@settings(max_examples=100)`

- [x] 22. CLI: `vsm replay` を実装する
  - [x] 22.1 `vsm/cli.py` に `replay` サブコマンドを追加する
    - `events.jsonl` を append 順で読み、各行 `<ts> <system_id> <channel_id> <event_type>` をスペース区切りで stdout (REQ 11.5)
    - active Run (`runs/{run_id}/RUNNING` lockfile 存在) の場合は stderr に warning を先出し (REQ 11.6)
    - Event_Log 不在時 stderr + exit code 2 (REQ 11.7)
    - _Requirements: 10.2, 11.5, 11.6, 11.7_
  - [x]* 22.2 `tests/unit/test_cli_replay.py` で出力フォーマットと active warning を検証する
    - フォーマット正規表現 + active 時に stderr に warning が出ることを fixture で検証
    - _Requirements: 11.5, 11.6_

- [x] 23. Checkpoint - CLI レイヤと PBT を通す
  - 19.x〜22.x の全テストが green であることを確認する。問題が発生したらユーザに確認する。

- [x] 24. 統合テスト: 代表シナリオ 12-success
  - [x] 24.1 `tests/integration/test_representative_scenario.py::test_scenario_success` を実装する
    - `FakeLLMProvider(latency=0.05)` で決定論的応答を設定し `vsm submit "..."` を実行
    - `events.jsonl` に S1 / S2 / S3 / S3* / S4 / S5 各 1 件以上の event と `s1_completion` が 1 件以上含まれることを検証 (REQ 12.7, 12.8)
    - 全プロセスが 1800 秒以内に正常終了 (REQ 12.9 不発)
    - 各 SLA (REQ 12.1〜12.6) が満たされることを timestamp で検証
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7, 12.8_

- [x] 25. 統合テスト: 代表シナリオ 12-timeout
  - [x] 25.1 `tests/integration/test_representative_scenario.py::test_scenario_timeout` を実装する
    - `FakeLLMProvider(latency=70)` + `FakeClock` で時間圧縮し 1800 秒経過を再現
    - exit code 6、stderr に欠落 System 名が含まれることを検証 (REQ 12.9)
    - Event_Log には開始時の `system_instantiated` まで含まれること
    - _Requirements: 12.9_

- [x] 26. 統合テスト: 代表シナリオ 12-replay-roundtrip
  - [x] 26.1 `tests/integration/test_representative_scenario.py::test_scenario_replay_roundtrip` を実装する
    - シナリオ 12-success 完了後の `events.jsonl` を `vsm replay` で読み出し、`replay()` の `ReconstructedState` がシナリオ実行中のキャッシュスナップショットと 4 projection で一致することを検証 (P5 の E2E 版)
    - `vsm replay` stdout の行数と `events.jsonl` の行数が一致すること
    - _Requirements: 10.10, 11.5_

- [x] 27. README と MVP Scope Boundaries ドキュメントを完成させる
  - [x] 27.1 `README.md` を完成させる
    - 概要、使い方 (`vsm submit` / `vsm status` / `vsm tail` / `vsm replay`)、`LITELLM_PROVIDER` などの環境変数説明
    - `MVP Scope Boundaries` セクションに REQ 14.1〜14.7 (FSX, publicness, shared surplus, human intervention, recursive growth, semi-stateful memory mixing, Web UI) の境界を列挙 (REQ 14.9)
    - Quick start: `pip install -e .` → `LITELLM_PROVIDER=openai vsm submit "hello"` → `vsm replay <run_id>`
    - _Requirements: 14.9_
  - [x]* 27.2 `tests/unit/test_readme.py` で `MVP Scope Boundaries` セクションの存在を smoke テストする
    - `README.md` に `## MVP Scope Boundaries` ヘッダと 14.1〜14.7 の主要キーワード (FSX 等) が含まれることを正規表現で確認
    - _Requirements: 14.9_

- [x] 28. Final checkpoint - 全テストと smoke を通す
  - 全 PBT (P1〜P17) と統合テスト 3 ケースが green、`vsm --help` が exit 0、`vsm submit "hello"` が `runs/<id>/events.jsonl` を生成することを確認する。問題が発生したらユーザに確認する。

## Notes

- `*` を付したサブタスクはオプションのテストタスク。MVP を素早く動かす場合はスキップ可能だが、全 PBT が green でないと Correctness Properties 検証は完了しない。
- 各タスクには対応する Requirement と Property を明示し、トレーサビリティを確保している。
- すべての PBT は `@settings(max_examples=100)` を付与する。
- LLM 呼び出しは原則 `FakeLLMProvider`、実 LLM は `@pytest.mark.live_llm` でゲート。
- `FakeClock` を `vsm/clock.py` に用意することで、SLA / タイムアウト / リトライ間隔のテストを決定論的に行う。
- スコープ外要求 (REQ 14.1〜14.7) は実装しない。Property 17 によって CLI 表面でも回帰検出する。
- Checkpoint タスク (7, 18, 23, 28) は依存グラフには含めず、UI 上での区切りに用いる。

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["2.1", "2.3", "2.5"] },
    { "id": 2, "tasks": ["2.2", "2.4", "2.6", "3.1", "4.1", "8.1"] },
    { "id": 3, "tasks": ["3.2", "4.2", "5.1", "8.2", "9.1"] },
    { "id": 4, "tasks": ["5.2", "5.3", "6.1", "8.3", "9.2", "9.3"] },
    { "id": 5, "tasks": ["6.2", "6.3", "8.4", "10.1", "11.1"] },
    { "id": 6, "tasks": ["10.2", "11.2", "11.3", "12.1", "13.1", "14.1", "15.1", "16.1", "17.1"] },
    { "id": 7, "tasks": ["12.2", "12.3", "13.2", "14.2", "15.2", "16.2", "17.2", "19.1"] },
    { "id": 8, "tasks": ["13.3", "19.2", "19.3", "20.1"] },
    { "id": 9, "tasks": ["20.2", "21.1"] },
    { "id": 10, "tasks": ["21.2", "22.1"] },
    { "id": 11, "tasks": ["22.2", "24.1"] },
    { "id": 12, "tasks": ["25.1"] },
    { "id": 13, "tasks": ["26.1"] },
    { "id": 14, "tasks": ["27.1"] },
    { "id": 15, "tasks": ["27.2"] }
  ]
}
```
