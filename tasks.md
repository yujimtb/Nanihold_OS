# VSM PoC Platform 実装タスクリスト

## 1. 基盤・共通モジュール

### 1.1 プロジェクト構造
- [ ] ディレクトリ構造の作成 (`vsm/`, `tests/`, `runs/`)
- [ ] `pyproject.toml` / `setup.py` の作成
- [ ] 依存関係の定義 (asyncio, typer, litellm, hypothesis, pytest)

### 1.2 共通ユーティリティ (`vsm/`)
- [ ] `ids.py`: UUIDv4 生成・バリデーション
- [ ] `clock.py`: UTC clock 抽象 (テスト容易化)
- [ ] `errors.py`: 例外階層の定義
- [ ] `config.py`: `vsm.toml` / 環境変数ローダ

## 2. メッセージング (`vsm/messaging/`)

### 2.1 チャネル定義
- [ ] `channels.py`: `ChannelId` enum の定義
- [ ] `channels.py`: `ALLOWED_ROUTES` 許容テーブルの定義
- [ ] `message.py`: `Message` dataclass の実装

### 2.2 Message_Bus
- [ ] `bus.py`: `MessageBus` クラスの実装
- [ ] `bus.py`: `send()` メソッド (ルート検証 + 配信)
- [ ] `bus.py`: `subscribe()` メソッド (キュー取得)
- [ ] `bus.py`: チャネル拒否時の `ChannelRejected` 処理

## 3. Event_Log (`vsm/eventlog/`)

### 3.1 スキーマ定義
- [ ] `schema.py`: 全 event_type の payload schema (pydantic)
- [ ] `schema.py`: 共通フィールド (`ts`, `run_id`, `event_type`, `seq`, `payload`)

### 3.2 Writer
- [ ] `writer.py`: `EventLogWriter` クラス
- [ ] `writer.py`: 単一 writer タスク (`_writer_loop`)
- [ ] `writer.py`: `append()` メソッド (queue 経由)
- [ ] `writer.py`: リトライロジック (最大3回, 100ms間隔)
- [ ] `writer.py`: fsync による永続化保証

### 3.3 Reader / Replay
- [ ] `reader.py`: JSONL 読み取り (tail 用)
- [ ] `replay.py`: `replay()` 関数
- [ ] `replay.py`: `ReconstructedState` dataclass
- [ ] `replay.py`: event_type ごとの apply handler

## 4. LLM Provider (`vsm/llm/`)

### 4.1 抽象化
- [ ] `types.py`: `LLMRequest` / `LLMResponse` dataclass
- [ ] `provider.py`: `LLMProvider` クラス (LiteLLM ラッパ)
- [ ] `provider.py`: `invoke()` メソッド (60秒タイムアウト)
- [ ] `provider.py`: プロバイダー差し替え対応 (`LITELLM_PROVIDER`)
- [ ] `provider.py`: エラーハンドリング (`LLMProviderError`, `LLMTimeoutError`)

## 5. System / Sub_Agent (`vsm/systems/`)

### 5.1 基底クラス
- [ ] `base.py`: `SubAgent` 基底クラス
- [ ] `base.py`: `SubAgent.respond()` メソッド (LLM呼び出し + タイムアウト)
- [ ] `base.py`: `System` 基底クラス
- [ ] `base.py`: `System.run()` メソッド (メインループ)
- [ ] `base.py`: `SystemRole` enum

### 5.2 S1_Worker
- [ ] `s1_worker.py`: `S1_Worker` クラス
- [ ] `s1_worker.py`: assignment 受信処理 (`S1_S3`)
- [ ] `s1_worker.py`: coordination directive 処理 (`S1_S2`)
- [ ] `s1_worker.py`: 監査要求処理 (`S3STAR_TO_S1`)
- [ ] `s1_worker.py`: 完了報告送信

### 5.3 S2_Coordinator
- [ ] `s2_coordinator.py`: `S2_Coordinator` クラス
- [ ] `s2_coordinator.py`: S1 状態監視
- [ ] `s2_coordinator.py`: `detect_conflict()` ロジック
- [ ] `s2_coordinator.py`: directive 生成 (5秒以内)
- [ ] `s2_coordinator.py`: ack タイムアウト検出 (30秒)

### 5.4 S3_Allocator
- [ ] `s3_allocator.py`: `S3_Allocator` クラス
- [ ] `s3_allocator.py`: `S1Pool` 管理
- [ ] `s3_allocator.py`: `find_idle()` メソッド (reuse 優先)
- [ ] `s3_allocator.py`: S1 動的生成 (最大64個)
- [ ] `s3_allocator.py`: assignment 送信 (1秒以内)
- [ ] `s3_allocator.py`: S5 への完了転送 (5秒以内)

### 5.5 S3Star_Auditor
- [ ] `s3star_auditor.py`: `S3Star_Auditor` クラス
- [ ] `s3star_auditor.py`: 観測スケジューラ (30秒 or 完了通知)
- [ ] `s3star_auditor.py`: S1 観測 (`S3STAR_TO_S1` 経由)
- [ ] `s3star_auditor.py`: finding 生成 (60秒以内)
- [ ] `s3star_auditor.py`: S5 への報告 (`S3STAR_S5_AUDIT`)

### 5.6 S4_Scanner
- [ ] `s4_scanner.py`: `S4_Scanner` クラス
- [ ] `s4_scanner.py`: 必須 Sub_Agent (営業, リサーチ) 登録
- [ ] `s4_scanner.py`: assessment 生成 (60秒以内)
- [ ] `s4_scanner.py`: Sub_Agent タイムアウト処理 (30秒)
- [ ] `s4_scanner.py`: S5 配送 (5秒以内)
- [ ] `s4_scanner.py`: 配送リトライ (最大3回, 10秒間隔)

### 5.7 S5_Policy
- [ ] `s5_policy.py`: `S5_Policy` クラス
- [ ] `s5_policy.py`: policy decision 生成
- [ ] `s5_policy.py`: 並行ディスパッチ (`asyncio.gather`)
- [ ] `s5_policy.py`: S3/S4 への配送 (各500ms以内)
- [ ] `s5_policy.py`: 片側失敗時の継続処理
- [ ] `s5_policy.py`: 監査報告受信処理

## 6. Runtime (`vsm/runtime/`)

### 6.1 State 管理
- [ ] `state.py`: `ReconstructedState` dataclass
- [ ] `state.py`: Tasks キャッシュ
- [ ] `state.py`: S1 pool キャッシュ

### 6.2 Lifecycle
- [ ] `lifecycle.py`: `start_run()` 関数
- [ ] `lifecycle.py`: 構造検証 (必須 System チェック)
- [ ] `lifecycle.py`: Run ディレクトリ作成
- [ ] `lifecycle.py`: System instantiation (5秒以内)
- [ ] `lifecycle.py`: Run 終了処理

## 7. Platform オーケストレータ

- [ ] `platform.py`: `VSM_Platform` クラス
- [ ] `platform.py`: `asyncio.run` エントリポイント
- [ ] `platform.py`: 全 System の起動・管理
- [ ] `platform.py`: Event_Log writer の起動
- [ ] `platform.py`: shutdown 処理 (cancel + flush)

## 8. CLI (`vsm/cli.py`)

### 8.1 submit コマンド
- [ ] `submit` サブコマンド実装
- [ ] description バリデーション (1-8192 ASCII)
- [ ] file 引数処理 (最大1MB, UTF-8)
- [ ] run_id / task_id 出力

### 8.2 観測コマンド
- [ ] `status` サブコマンド (replay ベース)
- [ ] `tail` サブコマンド (リアルタイム追従)
- [ ] `tail` フィルタ (system / channel)
- [ ] `replay` サブコマンド

### 8.3 エラーハンドリング
- [ ] 入力バリデーションエラー (exit code 2)
- [ ] 構造制約違反 (exit code 3)
- [ ] ディレクトリ作成失敗 (exit code 4)
- [ ] スコープ外機能拒否 (exit code 5)

## 9. Data Models

### 9.1 コアモデル
- [ ] `Task` / `TaskState` dataclass
- [ ] `Run` dataclass
- [ ] `S1State` dataclass
- [ ] `Conflict` / `CoordinationDirective` dataclass
- [ ] `EnvironmentAssessment` / `PolicyDecision` dataclass
- [ ] `AuditFinding` dataclass

## 10. テスト (`tests/`)

### 10.1 Unit Tests
- [ ] Message_Bus 単体テスト
- [ ] Event_Log Writer 単体テスト
- [ ] LLM Provider 単体テスト
- [ ] 各 System の単体テスト

### 10.2 Property-Based Tests (Hypothesis)
- [ ] P1: Channel rejection invariant
- [ ] P2: Channel delivery invariant
- [ ] P3: Event SLA conformance
- [ ] P4: Latency-bounded operation invariant
- [ ] P5: Event_Log round-trip
- [ ] P6: FIFO append order
- [ ] P7: Required field presence
- [ ] P8: S1 reuse vs instantiate dichotomy
- [ ] P9: Conflict detection correctness
- [ ] P10: Mandatory systems verification
- [ ] P11: Bounded counts
- [ ] P12: Tail filter semantics
- [ ] P13: CLI input validation
- [ ] P14: Audit schedule
- [ ] P15: Concurrent dispatch resilience
- [ ] P16: Retry semantics
- [ ] P17: Out-of-scope absence and rejection

### 10.3 Integration Tests
- [ ] 代表シナリオ 12-success (E2E)
- [ ] 代表シナリオ 12-timeout
- [ ] 代表シナリオ 12-replay-roundtrip

### 10.4 Smoke Tests
- [ ] `vsm --help` 動作確認
- [ ] `vsm submit` 基本動作
- [ ] `vsm status` 基本動作
- [ ] LLM Provider 初期化確認

### 10.5 テストインフラ
- [ ] `FakeLLMProvider` モック実装
- [ ] `FakeClock` 時間制御
- [ ] Hypothesis generators (Message, Event, S1State, etc.)
- [ ] テストフィクスチャ

## 11. ドキュメント

- [ ] README.md (MVP Scope Boundaries セクション含む)
- [ ] 使用方法ドキュメント
- [ ] 開発者ガイド
- [ ] API リファレンス

## 12. CI/CD

- [ ] pytest 実行設定
- [ ] coverage 計測 (目標80%)
- [ ] linter / formatter 設定
- [ ] GitHub Actions / CI パイプライン

## 実装優先順位の推奨

### Phase 1: 基盤 (Week 1-2)
1. プロジェクト構造
2. 共通ユーティリティ
3. メッセージング (Channel + Message_Bus)
4. Event_Log (Writer + Schema)

### Phase 2: System 実装 (Week 3-4)
5. System / Sub_Agent 基底クラス
6. LLM Provider
7. S1_Worker, S3_Allocator (最小構成)
8. S4_Scanner, S5_Policy (最小構成)

### Phase 3: 統合 (Week 5)
9. S2_Coordinator, S3Star_Auditor
10. Platform オーケストレータ
11. CLI (submit + status)
12. Runtime / Lifecycle

### Phase 4: テスト (Week 6-7)
13. Unit Tests
14. Property-Based Tests (優先度高いもの)
15. Integration Tests (代表シナリオ)

### Phase 5: 仕上げ (Week 8)
16. 残りの PBT
17. ドキュメント
18. CI/CD
19. 最終検証
