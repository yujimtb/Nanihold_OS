# chatbot-migration(移設 + B-3)

## ADDED Requirements

### Requirement: 無停止のチャットボット移設

システムは、現状凍結 → 並行稼働 → 会計接続 → 切替 → 監視期間 → 撤去の6手順でチャットボットを Nanihold へ移設し、切替を Event_Log に記録しなければならない (SHALL)。

#### Scenario: ゴールデンセットで両系を比較する
- **WHEN** 並行稼働で v1 と Nanihold 版の両系にゴールデンセットを投げる
- **THEN** 回答を比較できる

#### Scenario: 無停止で切り替わる
- **WHEN** 本番チャネルの向き先を切り替える
- **THEN** 検収基準と同水準の品質・応答時間を維持し、先方が切替に気づかない

### Requirement: 移行 Runbook の一般化

システムは、6手順を「既存の動いているサービスを Nanihold に吸収する標準手順」として Runbook 化しなければならない (SHALL)。

#### Scenario: 営業資料に転用できる
- **WHEN** Runbook を参照する
- **THEN** 既存組織のファジィな吸収の標準手順として β営業資料に再利用できる
