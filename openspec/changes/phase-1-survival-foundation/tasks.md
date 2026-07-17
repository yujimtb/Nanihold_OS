# Tasks — Phase 1: 生存基盤

> ロードマップ §5。Week A/B/C と 7/12 障害演習に対応。各タスクの DoD は対応 spec の Scenario を正とする。

## 再基線化判定 (2026-07-18)

現行 Nanihold リポジトリのコード、テスト、実装結果文書を照合したが、Phase 1 の DoD（円建て
CostRecorded、日次・月次 cap、3経路 kill switch、常駐復旧、一般 scheduler、Discord 生存レポート、
6シナリオ障害演習など）を完了と断定できる証拠は得られなかった。そのため、以下の `[ ]` は過剰な
完了マークを避けて変更せず、要確認リストに残す。Wave 3〜5 の実装完了はこの Phase 1 の生存基盤
DoD の代替根拠とはしない。

### 要確認リスト（31件）

- Week A: `1.1`〜`1.9`（Web UI の外部非公開境界、円建て課金、日次/月次 cap、kill switch、
  systemd/health 監視、3段階 algedonic の各 DoD）
- Week B: `2.1`〜`2.8`（一般 scheduler/catch-up、生存レポート、watchdog、権限付き lifecycle、
  suspend 安全停止、Discord human review、顧客入力の敵対テスト CI 常設）
- Week C: `3.1`〜`3.5`（org/customer schema、org 別会計、受託 lifecycle、請求/入金、model tiering）
- 障害演習: `4.1`〜`4.7`（6シナリオの実行証跡と営業可能状態の判定）
- 並行・事業: `5.1`〜`5.2`（開業・会計および HUMAI 奨励金の外部確認）

## 1. Week A: 死なない基盤(6/25–7/1)
- [ ] 1.1 Web UI ブランチをマージし常駐に同梱(LAN/VPN 内のみ、外部非公開)— `web-ui`
- [ ] 1.2 LiteLLM cost callback → CostRecorded → BudgetLedger 集計、円換算(月初固定レート)— `cost-accounting`
- [ ] 1.3 vsm CLI / Web UI で Run コストを円で即答、ダミー Run 10本で合計一致 — `cost-accounting`
- [ ] 1.4 日次・月次上限(org+グローバル)を発行前/CostRecorded 時の二段で判定 — `budget-cap`
- [ ] 1.5 枯渇時: 拒否→suspend→EscalationFacade→algedonic。自動引き上げ経路を作らない — `budget-cap`
- [ ] 1.6 kill switch を CLI / Web UI / Discord の3経路で実装、発動を記録 — `kill-switch`
- [ ] 1.7 systemd unit(Restart=on-failure)、起動時 replay、stale ロック回収 — `runtime-resilience`
- [ ] 1.8 /health 外形監視(LETHE+Nanihold)とダウン通知 — `runtime-resilience`
- [ ] 1.9 algedonic WARN/ALERT/PAGE を階層バイパスで実装、通知ループ防止 — `algedonic-alerts`

## 2. Week B: 自律の最小骨格(7/2–7/8)
- [ ] 2.1 cron/interval schedule、ScheduleFired→Run、schedule_id+予定時刻で冪等化 — `scheduler`
- [ ] 2.2 catch-up「最新1回のみ実行」デフォルト — `scheduler`
- [ ] 2.3 日次生存レポートを S4 定期 Run で生成し Discord 投稿、3日連続・数値一致 — `survival-report`
- [ ] 2.4 ハートビート監視、30分無活動で ALERT+自動 suspend — `watchdog`
- [ ] 2.5 terminate/suspend/resume を CONTROL effect、ParentAuthority 検証 — `lifecycle-facades`
- [ ] 2.6 suspend は実行中 invocation 完了待ち、即時中断は terminate 限定 — `lifecycle-facades`
- [ ] 2.7 Discord 承認(✅/❌、許可ユーザー限定)、期限超過 escalation — `human-review`
- [ ] 2.8 顧客入力をデータ扱い、scope→sandbox 変換の敵対テスト3種を CI 常設 — `input-isolation`

## 3. Week C: 商売の配管(7/9–7/12)
- [ ] 3.1 EventEnvelope に org_id(必須)/customer_id/provenance 追加、replay 互換 — `org-customer-schema`
- [ ] 3.2 BudgetLedger を org 別集計に拡張 — `org-customer-schema`
- [ ] 3.3 受託ライフサイクル7イベント、顧客別損益 projection、過去案件の遡及登録 — `engagement-lifecycle`
- [ ] 3.4 請求・入金を手動 CLI 記録、未請求/入金待ちを日次レポート表示 — `engagement-lifecycle`
- [ ] 3.5 LiteLLM モデルエイリアスで役割別階層、モデル別コスト内訳 — `model-tiering`

## 4. 障害演習(7/12)
- [ ] 4.1 LLM プロバイダ全断(API キー無効化)→ fallback or 安全停止
- [ ] 4.2 予算枯渇(キャップ 100円)→ N-2 挙動
- [ ] 4.3 Run ハング → N-8 検出
- [ ] 4.4 プロセス kill -9 → N-4 復旧
- [ ] 4.5 kill switch → N-3 全停止と再開
- [ ] 4.6 Discord 不達(webhook 無効化)→ 通知失敗の検出手段確認
- [ ] 4.7 6シナリオの結果を記録(運用 Runbook 初版とする)= **マイルストーン: 営業可能状態**

## 5. 並行・事業タスク
- [ ] 5.1 B-2: 開業届・請求書様式・会計(freee 等)整備(期限 7/12)
- [ ] 5.2 B-8: HUMAI 奨励金の使途規定確認(6月中)
