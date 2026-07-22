# ACR-08 オーナー実施手順チェックリスト

対象は owner→agent 16セルと agent→owner 4セルの計20セル。各行の文言をそのまま使い、実施後に `results.json` へ実際のIDを記録する。実Discord/実Slackへの実送信はオーナー承認後の別作業であり、このWorkItemでは行わない。

`agent_to_owner` の4セルは、固定60セルの宛先解決軸から `explicit_mention` を代表行として使う。返信の実際の検証対象は宛先解決ではなく、起草者個名・`reply-approval@1`・既存bridgeのsend-recordである。

## 共通記録

- 通知セル: `status`, `channel`, `source_message_id`, `notification_id`, `audit_trace_subject`
- 観測のみセル: `status`, `observation_subject`。同じsubjectの通知IDは空欄にする
- 返信セル: `status`, `channel`, `draft_id`, `approval_id`, `send_record_id`, `audit_trace_subject`
- `results.json` の `real_external_sends_performed` と `evidence.json` の `allow_external_send` は省略せず、明示的なbooleanにする（このWorkItemの値はともに `false`）
- 判定はLedgerの `agent_notification_delivered` または既存 `vsm audit-trace` の `verified: true` を根拠にする
- bot返信は指定されたbotメッセージへの返信、スレッド継承は先に指定個名宛ての親を作ってから返信する

## 送信・期待結果

| # | Cell ID | チャネル | 宛先解決/種別 | 送る文言 | 操作 | 期待結果 |
|---:|---|---|---|---|---|---|
| 1 | `ACR08-DISCORD-EXPLICIT_MENTION-NAGI-OWNER_TO_AGENT` | discord | explicit / Nagi | `@Nagi ACR08 discord explicit_mention nagi` | チャネルへ送信 | Nagi宛の通知、Ledger Event、ACR-04 trace verified |
| 2 | `ACR08-DISCORD-EXPLICIT_MENTION-TASK_AGENT-OWNER_TO_AGENT` | discord | explicit / task | `@<割当済み個名> ACR08 discord explicit_mention task_agent` | 実行個名へ置換して送信 | 指定個名宛の通知、Ledger Event、ACR-04 trace verified |
| 3 | `ACR08-DISCORD-BOT_REPLY_ATTRIBUTED-NAGI-OWNER_TO_AGENT` | discord | bot返信帰属あり / Nagi | `ACR08 discord bot_reply_attributed nagi bot attribution reply` | Nagi帰属botへ返信 | bot帰属が保持されNagiへ通知、Ledger Event、trace verified |
| 4 | `ACR08-DISCORD-BOT_REPLY_ATTRIBUTED-TASK_AGENT-OWNER_TO_AGENT` | discord | bot返信帰属あり / task | `ACR08 discord bot_reply_attributed task_agent bot attribution reply` | 実行個名帰属botへ返信 | bot帰属の実行個名へ通知、Ledger Event、trace verified |
| 5 | `ACR08-DISCORD-BOT_REPLY_UNATTRIBUTED-NAGI-OWNER_TO_AGENT` | discord | bot返信帰属不能 / Nagi | `ACR08 discord bot_reply_unattributed nagi unattributed bot reply` | 帰属不能botへ返信 | 観測落ちせずNagiへ通知、Ledger Event、trace verified |
| 6 | `ACR08-DISCORD-THREAD_INHERITANCE-NAGI-OWNER_TO_AGENT` | discord | thread継承 / Nagi | `ACR08 discord thread_inheritance nagi inherited thread reply` | Nagi宛親のスレッドへ返信 | 親のNagi宛先を継承、Ledger Event、trace verified |
| 7 | `ACR08-DISCORD-THREAD_INHERITANCE-TASK_AGENT-OWNER_TO_AGENT` | discord | thread継承 / task | `ACR08 discord thread_inheritance task_agent inherited thread reply` | 実行個名宛親のスレッドへ返信 | 親の実行個名宛先を継承、Ledger Event、trace verified |
| 8 | `ACR08-DISCORD-OBSERVATION_ONLY-NAGI-OWNER_TO_AGENT` | discord | 非合致 / 観測のみ | `ACR08 discord observation_only nagi observation-only message` | 宛先記法なしで送信 | LETHE観測のみ、agent_notification_deliveredなし、誤配送なし |
| 9 | `ACR08-SLACK-EXPLICIT_MENTION-NAGI-OWNER_TO_AGENT` | slack | explicit / Nagi | `@Nagi ACR08 slack explicit_mention nagi` | チャネルへ送信 | Nagi宛の通知、Ledger Event、ACR-04 trace verified |
| 10 | `ACR08-SLACK-EXPLICIT_MENTION-TASK_AGENT-OWNER_TO_AGENT` | slack | explicit / task | `@<割当済み個名> ACR08 slack explicit_mention task_agent` | 実行個名へ置換して送信 | 指定個名宛の通知、Ledger Event、ACR-04 trace verified |
| 11 | `ACR08-SLACK-BOT_REPLY_ATTRIBUTED-NAGI-OWNER_TO_AGENT` | slack | bot返信帰属あり / Nagi | `ACR08 slack bot_reply_attributed nagi bot attribution reply` | Nagi帰属botへ返信 | bot帰属が保持されNagiへ通知、Ledger Event、trace verified |
| 12 | `ACR08-SLACK-BOT_REPLY_ATTRIBUTED-TASK_AGENT-OWNER_TO_AGENT` | slack | bot返信帰属あり / task | `ACR08 slack bot_reply_attributed task_agent bot attribution reply` | 実行個名帰属botへ返信 | bot帰属の実行個名へ通知、Ledger Event、trace verified |
| 13 | `ACR08-SLACK-BOT_REPLY_UNATTRIBUTED-NAGI-OWNER_TO_AGENT` | slack | bot返信帰属不能 / Nagi | `ACR08 slack bot_reply_unattributed nagi unattributed bot reply` | 帰属不能botへ返信 | 観測落ちせずNagiへ通知、Ledger Event、trace verified |
| 14 | `ACR08-SLACK-THREAD_INHERITANCE-NAGI-OWNER_TO_AGENT` | slack | thread継承 / Nagi | `ACR08 slack thread_inheritance nagi inherited thread reply` | Nagi宛親のスレッドへ返信 | 親のNagi宛先を継承、Ledger Event、trace verified |
| 15 | `ACR08-SLACK-THREAD_INHERITANCE-TASK_AGENT-OWNER_TO_AGENT` | slack | thread継承 / task | `ACR08 slack thread_inheritance task_agent inherited thread reply` | 実行個名宛親のスレッドへ返信 | 親の実行個名宛先を継承、Ledger Event、trace verified |
| 16 | `ACR08-SLACK-OBSERVATION_ONLY-NAGI-OWNER_TO_AGENT` | slack | 非合致 / 観測のみ | `ACR08 slack observation_only nagi observation-only message` | 宛先記法なしで送信 | LETHE観測のみ、agent_notification_deliveredなし、誤配送なし |
| 17 | `ACR08-DISCORD-EXPLICIT_MENTION-NAGI-AGENT_TO_OWNER` | discord | draft / Nagi起草 | `ACR08 discord explicit_mention nagi reply-draft body` | Nagiに起草させ、card-queue確認後にownerが承認 | draft→`reply-approval@1`→既存bridgeのsend-record、承認前送信0 |
| 18 | `ACR08-DISCORD-EXPLICIT_MENTION-TASK_AGENT-AGENT_TO_OWNER` | discord | draft / task起草 | `ACR08 discord explicit_mention task_agent reply-draft body` | 実行個名に起草させ、card-queue確認後にownerが承認 | draft→`reply-approval@1`→既存bridgeのsend-record、承認前送信0 |
| 19 | `ACR08-SLACK-EXPLICIT_MENTION-NAGI-AGENT_TO_OWNER` | slack | draft / Nagi起草 | `ACR08 slack explicit_mention nagi reply-draft body` | Nagiに起草させ、card-queue確認後にownerが承認 | draft→`reply-approval@1`→既存bridgeのsend-record、承認前送信0 |
| 20 | `ACR08-SLACK-EXPLICIT_MENTION-TASK_AGENT-AGENT_TO_OWNER` | slack | draft / task起草 | `ACR08 slack explicit_mention task_agent reply-draft body` | 実行個名に起草させ、card-queue確認後にownerが承認 | draft→`reply-approval@1`→既存bridgeのsend-record、承認前送信0 |

## 自動検証

実施結果を `results.json`、Ledgerの `/api/events` 読取結果と既存監査トレースを `evidence.json` に保存し、次を実行する。

```powershell
python scripts/acr08_connectivity.py verify `
  --results results.json `
  --evidence evidence.json `
  --output acr08-verification.json
```

このコマンドは全26適用セルが揃っていること、N/Aセルが結果に混入していないこと、Ledger Eventと監査トレースのID・宛先・個名帰属が一致すること、観測のみの誤配送がないことを検証する。検証中にEventや外部送信を追加しない。
