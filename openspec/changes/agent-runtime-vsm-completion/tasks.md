# Tasks: 実装ウェーブ分割

design.md の §番号を参照。各ウェーブは単独でテスト緑・コミット可能な単位とする。

## Wave 1: AgentRuntime 基盤(§1, §2)
- [x] 1.1 `vsm/agents/runtime.py`: AgentRequest / AgentResult / AgentRuntimeProtocol / AgentRuntimeError
- [x] 1.2 `vsm/agents/backends/`: claude_code.py / codex.py / litellm_adapter.py / fake.py(process_factory 注入、JSON/JSONL パース、quota 検知を含む)
- [x] 1.3 `vsm/config.py`: `[agents]` `[session]` 節のロード(AgentsConfig)。env 上書き(CLAUDE_BIN / CODEX_BIN)
- [x] 1.4 `SubAgent.respond` を AgentRuntime 経由に置換。イベント payload 拡張(backend / session_ref / tokens_cache_read、schema_version 更新)。タイムアウトのバックエンド別化
- [x] 1.5 Platform.create のロール別 runtime 解決と注入。Web の FakeLLM フォールバックも fake runtime に統一
- [x] 1.6 テスト(モックサブプロセスでの両CLIパーサ、ロール別解決、既存テスト全緑)

## Wave 2: Budget + クォータ自動復帰(§3, §5-6)
- [x] 2.1 `[budget]` ロード、ParentAuthority.budget_envelope / NodeRunState.budget への注入
- [x] 2.2 budget_consumed イベント + NodeRunState.cost_consumed 累算(トークン3種 + wall clock)
- [x] 2.3 超過時の拒否 + budget_exceeded + escalation
- [x] 2.4 `vsm status` / `vsm runs` への消費表示
- [x] 2.5 `vsm/runtime/quota.py` QuotaMonitor: quota_exhausted → Node SUSPENDED → reset 時刻(or fallback)で自動 resume + 保留メッセージ再投入
- [x] 2.6 テスト(強制・累算・suspend/resume・メッセージ非消失)

## Wave 3: トークン削減(§4)
- [ ] 3.1 `vsm/memory/builder.py` ContextViewBuilder
- [ ] 3.2 S1 完了時の TaskSummary 生成 + TaskSummaryIndex 登録
- [ ] 3.3 NodeRunState.session_refs、Run内・Node内セッション再開、resume 失敗時の新規フォールバック
- [ ] 3.4 テスト(ビュー構築の決定論、resume 経路、フォールバック)

## Wave 4: S2 AI 調停 + Algedonic + Consortium(§7, §8, §9)
- [ ] 4.1 S2Coordinator の AI 調停ハンドラ(設定でオンオフ)
- [ ] 4.2 ChannelId.ALGEDONIC + raise_algedonic facade + S5 ハンドラ + Human 通知フック
- [ ] 4.3 `vsm/runtime/consortium.py`(参加者=Node参照+Human、ラウンド制、タイムアウトポリシー)
- [ ] 4.4 招集トリガ3種(S5 任意 / algedonic 連動 / 人間発)
- [ ] 4.5 テスト(調停決定、バイパス配送、合議プロトコル、human timeout)

## Wave 5: API + WebUI(§10, §11)
- [ ] 5.1 REST エンドポイント6種 + instruction_received 配送
- [ ] 5.2 `vsm instruct` CLI
- [ ] 5.3 topology projection API
- [ ] 5.4 React「組織図」ビュー(Node カード、状態色分け、予算バー、介入操作)
- [ ] 5.5 ドキュメント更新(README 起動手順一本化、implementation-status.md、ROADMAP.md)
