# Nanihold OS ロードマップ

- 更新日: 2026-07-18
- 作成: Claude (自律作業セッション)。上流(node event model 世代)の実態調査・2026-06 のロードマップ議論・07-08〜09 の統合作業に基づく

## いまの姿 (2026-07-18 時点)

- Stafford Beer の VSM(Viable System Model)に基づく「AI 自動会社」ランタイム。2026-06 中旬の大規模リファクタで **Architecture / Role / Agent / Tool / Node の5層構成+ノードイベントモデル**に進化した
- **Webダッシュボード**(FastAPI + React/Vite)が実働: 日本語UIで JSON タスク投入、実行アーカイブ、Event_Log 由来のライブ組織図、Node/人間介入、予算消費を確認できる
- ツールファサード 11 種(codex_run、search、spawn、human、escalation など)
- CLI(`vsm submit / instruct / status / tail / replay / runs`)から Run 投入と実行中 Node への外部指示が可能
- AgentRuntime、Budget/quota、ContextView/Node内session resume、S2 AI調停、Algedonic、Consortium、Wave 5 API/UI、selfdev Wave 3/4 の基盤まで実装済み。Consortium statement は日本語ファーストの backend prompt を使用する

## 2026-07-09 の統合作業(夜間UX改善の上流移植)

旧ベース(6/2 時点)で実装した UX 改善を、上流の新アーキテクチャへ手動移植した(機械マージは ASCII 制限の正反対決定があり不採用):
1. `vsm runs` 新設、`vsm status`/`replay` の人間可読化(イベントからの状態導出)、`vsm submit` の進捗表示、エラーの平文化
2. **実バグ修正: S5→S4→S5 フィードバックループによるテスト shutdown 40秒超ハング**(上流現行版で再現)を S4+S5 両停止で解消
3. **実バグ修正: pytest 並走時の一時ディレクトリ相互破壊**(固定 basetemp 共有が真因。決定論的に再現)→ PID 付きサブディレクトリ方式に変更(このマシンでは pytest 既定 Temp が権限エラーになるため repo 内 PID 方式を採用)
4. Web の artifact パスが Windows 区切り(\)で記録される不具合を / 固定に修正
5. dev / bot extras の分離(discord.py はテスト収集を skip)、smoke run を通常の `runs/<run_id>` に統一(CLI から一気通貫で確認可能)

移植しなかったもの: ASCII 制限まわりの旧文言(上流が日本語 description を解禁済みのため、持ち込むと退行)。旧ローカルコミットはブランチ `archive/overnight-ux-20260708` に保全。

## 次の一歩(短期、2026-07-18 再基線化後)

### 2026-07-18 D30 Survival Wave 0〜2

- Wave 0〜2の円建て operational ledger、runtime usage metering、日次 survival report、
  `/api/survival/dashboard`、Web UI「事業状況」タブを追加した。
- 実価格・為替の登録、全量の Docker Compose pytest/frontend build は未実行であり、
  `config/survival-pricing.toml.example` の `要オーナー確定` を埋めるまで usage は unpriced として表示する。
- 外部送信・実請求・Human認証はこのレーンの対象外で、loopback安全境界を維持する。

### 自己開発ループ Wave 3/4 (2026-07-13 以降)

Proposal の Domain / State / Event / Store 基盤、workspace / GateRunner v2 / candidate commit、headless controller、Consortium、durable Human waiter、S3★ audit、repair 1回制限、scheduler、および Wave 4 の API / CLI / WebUI / FastAPI lifespan 配線を接続済み。詳細は [Wave 3 実装結果](openspec/changes/selfdev-loop/wave3-result.md) と [Wave 4 実装結果](openspec/changes/selfdev-loop/wave4-result.md) を参照する。

1. **Phase 1 生存基盤の再確認** — 円建て CostRecorded、日次/月次 cap、kill switch、常駐復旧、scheduler、survival report、watchdog、障害演習の完了根拠を揃える
2. **CLI Run と Web Run の統合** — 現状 CLI(`runs/<id>/`)と Web(`runs/web/<id>/runtime/<runtime_id>/`)は別ディレクトリ・別 run_id 体系。Event_Log projection を共有できる単一 Run identity へ寄せる
3. **Human review の待機継続** — API から回答イベントを記録する段階から、要求元 Tool の Future を解決して作業を継続する正式な待機プロトコルへ拡張する
4. **Run 間の会計・長期記憶** — 現在の Run 内 Budget/ContextView を、Node と LETHE を介した長期運用へ接続する

## 2026-07-12 Wave 5 完了

- `POST /api/runs` を `goal` / `constraints` / `budget` の JSON 契約へ統一
- Human→Node の追加指示 Message と `vsm instruct` を追加
- Algedonic / Consortium statement / topology / budget API を公開
- Event_Log だけから再構成できるライブ組織図と Node 介入 UI を追加
- README の標準起動手順を Docker Compose app + uvicorn + Vite に一本化

## 2026-07-18 live instruct / shutdown 修正

- `vsm instruct` の未適用指示を対象 Node の次の LLM invocation 開始前に必ず注入し、
  `instruction_applied` に適用先 invocation ID を記録する。
- EventLogWriter の受付終了と sentinel を原子的にし、shutdown と Message 送信の競合でも
  停止前に受理済みの event を全件排水する。
- System の受信子 Task、Web generation Task、AgentRuntime CLI process を cancel 後にawaitし、
  Platform / FastAPI lifespan の shutdown を一度だけ完遂する。
- 回帰根拠は `tests/unit/test_instruction_shutdown_races.py`、`tests/unit/test_web.py`、
  `tests/unit/test_agent_backends.py` に置く。

## 2026-07-18 再基線化の判定

### 完了根拠

- selfdev Wave 3 の headless controller / Consortium / audit / scheduler は [Wave 3 実装結果](openspec/changes/selfdev-loop/wave3-result.md) と commit `67f7b01`、Wave 4 の API / CLI / WebUI は [Wave 4 実装結果](openspec/changes/selfdev-loop/wave4-result.md) と commit `4a13538` に記録されている。
- Wave 5 の REST / CLI / 組織図 UI は [Wave 5 実装結果](openspec/changes/agent-runtime-vsm-completion/wave5-result.md) と commit `a40e9e0` 以降に記録されている。
- 通常 Consortium の role prompt と selfdev Consortium backend prompt の日本語ファースト契約は [Role prompt 調整結果](openspec/changes/agent-runtime-vsm-completion/role-prompts-result.md) と `tests/unit/test_role_prompts.py` / `tests/unit/test_selfdev_wave3.py` に記録されている。
- Phase 0 chatbot delivery の旧32項目は、ask_bot の後継実装・実環境 gate・全量投入へ置換済みとして [Phase 0 tasks](openspec/changes/phase-0-chatbot-delivery/tasks.md) に完了根拠を付した。判定の一次根拠は `D:\userdata\docs\projects\_mission_20260712_nanihold\BACKLOG_20260718.md` である。

### 要確認・未完了

- Phase 1 生存基盤の31項目は、現行コードだけでは仕様の全 DoD を満たしたと断定できないため、[Phase 1 tasks](openspec/changes/phase-1-survival-foundation/tasks.md) の `[ ]` と要確認リストを維持する。
- CLI/Web の単一 Run identity、Human review Future の正式再開、Run 間会計・長期記憶は Wave 5 結果文書に残る未完了項目である。

## 中期(LETHE / Eos との接続)

- S4(環境スキャン)の情報源として LETHE(個人データレイク)を使う — 「会社」が持ち主の実データを観測できるようになる
- Run 間の長期記憶を独自実装せず、LETHE への observation / supplemental 書き込みで代替する
- 人間承認(human ツール)を Eos の reply cards / 承認フローと共通化する

## 長期の方向性

「AI 自動会社」— 自律的に環境を観測し(S4)、方針を保ち(S5)、資源を配分し(S3)、実務を行う(S1)組織体。MVP スコープとして保留していた項目(FSX 数値最適化、公共性測定、共有剰余配分、再帰的サブ VSM)は、コスト可視化と Run 統合で運用が安定してから。

## やらないこと(現時点)

- S1 の無制限な実行能力(codex_run ファサードの承認・サンドボックス設計を超える拡張)
- ASCII 制限の復活(日本語ファーストで進む)
