# Tasks — Phase 2: 営業開始と移設

> ロードマップ §6。7月中旬→下旬→8月。マイルストーンは 8/31 収益 > 燃焼。

## 1. persistent サービス Node(7月中旬)
- [ ] 1.1 起動・停止ライフサイクルイベント(ServiceNodeStarted/Stopped)— `persistent-service-node`
- [ ] 1.2 ハートビート(watchdog 接続)とクラッシュ時自動再起動(起動回数記録)— `persistent-service-node`
- [ ] 1.3 リクエスト単位会計(1質問=1課金単位、CostRecorded に customer_id)— `persistent-service-node`
- [ ] 1.4 設計判断を ADR として記録(将来のサービス型商品の雛形)

## 2. lethe_query facade(7月中旬)
- [ ] 2.1 lethe_query(EXTERNAL_READ)、network_scope を LETHE URL のみに制限 — `lethe-query-facade`
- [ ] 2.2 scope 外 URL 接続がテストで失敗することを担保 — `lethe-query-facade`
- [ ] 2.3 読取結果は ToolInvocation 記録と派生結論のみ保存、生データ非保存 — `lethe-query-facade`

## 3. チャットボット移設(7月下旬)
- [ ] 3.1 現状凍結(v1 構成・環境変数・依存の文書化)— `chatbot-migration`
- [ ] 3.2 並行稼働(テストチャネルでゴールデンセット両系比較)— `chatbot-migration`
- [ ] 3.3 会計接続(質問単位コストが customer_id 付きで BudgetLedger に)— `chatbot-migration`
- [ ] 3.4 切替(本番チャネルの向き先変更、切替を Event_Log 記録)— `chatbot-migration`
- [ ] 3.5 監視期間1週間(v1 を即時切り戻し可能に温存)→ 撤去・トークンローテーション
- [ ] 3.6 B-3: 6手順を「既存サービス吸収の標準手順」として Runbook 化(営業資料転用)

## 4. 受託営業の開始(8月)
- [ ] 4.1 商品定義(受ける/受けない/価格)を S5 ポリシーとして明文化 — `sales-intake`
- [ ] 4.2 定義外の受注を human review 必須に(N-10 適用対象へ追加)— `sales-intake`
- [ ] 4.3 受注導線: Discord bot 拡張 + 簡易フォーム、LeadRegistered→見積もりドラフト→review→EstimateIssued — `sales-intake`
- [ ] 4.4 獲得活動: SHIMOKITA 追加案件の打診、知人筋への声かけ(目標: 8月中に受託2件目検収)
- [ ] 4.5 自社タスク投入開始(週次レビュー・日次レポート・リファクタを Run 化、デモデータ蓄積)

## 5. 効率化と自律化(8月)
- [ ] 5.1 過去 Run の索引化(v1 全文検索)、分解時に再利用候補提示 — `subtask-reuse`
- [ ] 5.2 代表シナリオ3本(A/B/C)を固定フローで回帰固定 — `dynamic-differentiation`
- [ ] 5.3 分化判断ポリシー(複雑度見積×budget 残)を S5/S3 に実装 — `dynamic-differentiation`
- [ ] 5.4 フラグで動的切替→3シナリオ+実受託1件で比較→動的をデフォルト化 — `dynamic-differentiation`

## 6. 体制・事業タスク
- [ ] 6.1 友人合流(院試後): L-7 で立ち上げ
- [ ] 6.2 B-5: セキュリティレビュー第1回(認証・sandbox・シークレットを AWS 観点で総点検)
