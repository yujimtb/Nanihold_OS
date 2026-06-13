# Tasks — Phase 3: 観測層統合

> ロードマップ §7。LETHE 側(友人)と Nanihold 側(本人)を並行。マイルストーンは 9/30 観測ループ稼働。

## 1. LETHE: org/tenant 抽象(L-4)
- [ ] 1.1 observation/identity/projection/API すべてに org_id 導入 — `lethe-tenant`
- [ ] 1.2 SQLite をテナントごとにファイル分離(`data/{org_id}/lethe.sqlite3`)— `lethe-tenant`
- [ ] 1.3 トークンを org に紐付け、org をまたぐクエリを存在させない — `lethe-tenant`
- [ ] 1.4 2 org(自社+ダミー)同居で、トークン A が org B に到達不能なテスト — `lethe-tenant`

## 2. LETHE: スキーマ設定化(L-5)
- [ ] 2.1 dorm 前提(person page 構成・property)を org ごとの宣言的定義(TOML/YAML)に外出し — `lethe-schema-config`
- [ ] 2.2 設定差替えで研究コミュニティ用スキーマ(HUMAI 想定)の page 生成 — `lethe-schema-config`

## 3. LETHE: Discord adapter(L-6)
- [ ] 3.1 Slack adapter と同型(channel 指定・thread 追跡・observation 化)— `lethe-discord-adapter`
- [ ] 3.2 自社 Discord 指定チャンネルが Lake に入り、identity が Slack 側人物と統合 — `lethe-discord-adapter`

## 4. LETHE: IngestionGate policy 接続(L-7)— ※友人合流初週の立ち上げタスク
- [ ] 4.1 取り込み時点で consent policy 評価、オプトイン外を保存前に破棄 — `lethe-ingestion-gate`
- [ ] 4.2 オプトイン外チャンネルのメッセージが Lake に存在しないことを直接検証 — `lethe-ingestion-gate`

## 5. LETHE: retention / 削除 API(L-8)
- [ ] 5.1 人単位削除(observation/identity/projection/blob 連鎖削除 + AuditLog 記録)— `lethe-retention`
- [ ] 5.2 保持期間ポリシー(org 設定)による自動失効 — `lethe-retention`
- [ ] 5.3 ダミー人物削除後、全 API・全ストレージから消え削除記録だけ残るテスト — `lethe-retention`
- [ ] 5.4 チャットボット運用手順書の削除窓口をこの API 呼び出しに更新

## 6. LETHE: write-back 変換層(L-9)
- [ ] 6.1 Command/EffectPlan → ToolInvocation(EXTERNAL_WRITE)変換 — `lethe-write-back`
- [ ] 6.2 LETHE 側は write 用エンドポイント(`write:` scope)と AuditLog 記録のみ — `lethe-write-back`
- [ ] 6.3 Notion 1フィールド更新が human review 承認経てのみ反映の E2E — `lethe-write-back`

## 7. Nanihold: ObservedTopology(N-20)
- [ ] 7.1 ObservedPerson/Role/Edge/Unit/HumanCorrection のデータモデル — `observed-topology`
- [ ] 7.2 役割を排他ラベルでなく重み分布で表現 — `observed-topology`
- [ ] 7.3 LETHE timeline からの派生イベントで projection 構築、Web UI に信頼度付き表示 — `observed-topology`

## 8. Nanihold: O3 構造推論バッチ(N-21)
- [ ] 8.1 特徴抽出(活動統計、LLM 不使用)— `structure-inference`
- [ ] 8.2 役割分類(安価 LLM、VSM 用語は人間向け表示まで出さない)— `structure-inference`
- [ ] 8.3 集約(指数移動平均で急変抑制)+ HumanCorrection を制約反映 — `structure-inference`
- [ ] 8.4 週次バッチ・org あたり上限額(budget-cap 配下)— `structure-inference`

## 9. Nanihold: O5 確認ループ UI(N-22)
- [ ] 9.1 グラフ表示→各推定クリック→合/違/わからない→HumanCorrection — `human-correction-loop`
- [ ] 9.2 1判断10秒以内、修正が即時反映され次回推論で尊重(E2E)— `human-correction-loop`

## 10. Nanihold: O6 自動化レベル(N-23)
- [ ] 10.1 Lv0〜Lv3 を ObservedUnit 単位の設定、初期値 Lv0 — `automation-levels`
- [ ] 10.2 引き上げは human review 必須 + Event_Log 記録 — `automation-levels`
- [ ] 10.3 Lv0 unit への EXTERNAL_WRITE が構造的に不可能なテスト — `automation-levels`

## 11. セルフ観測の開始(8月中・最重要)
- [ ] 11.1 自社 Discord/Slack を取り込み、N-20/21 を自社相手に常時運用
- [ ] 11.2 9月以降の本人稼働低下に備え、観測層を自社で回す(改善をバッチ調整作業化)
