# ACR-08 実経路疎通試験マトリクス

`チャネル(2) × 宛先解決(5) × 宛先種別(2) × 方向(3) = 60セル`を固定順で列挙する。適用は26セル、N/Aは34セルである。このWorkItemでは実Discord/実Slackへの実送信を行わない。

| # | Cell ID | チャネル | 宛先解決 | 種別 | 方向 | 判定 | 実施形態 | N/A理由 |
|---:|---|---|---|---|---|---|---|---|
| 1 | `ACR08-DISCORD-EXPLICIT_MENTION-NAGI-OWNER_TO_AGENT` | discord | explicit_mention | nagi | owner_to_agent | 適用 | owner_checklist | — |
| 2 | `ACR08-DISCORD-EXPLICIT_MENTION-NAGI-AGENT_TO_OWNER` | discord | explicit_mention | nagi | agent_to_owner | 適用 | owner_checklist | — |
| 3 | `ACR08-DISCORD-EXPLICIT_MENTION-NAGI-AGENT_TO_AGENT` | discord | explicit_mention | nagi | agent_to_agent | 適用 | automated_dry_run | — |
| 4 | `ACR08-DISCORD-EXPLICIT_MENTION-TASK_AGENT-OWNER_TO_AGENT` | discord | explicit_mention | task_agent | owner_to_agent | 適用 | owner_checklist | — |
| 5 | `ACR08-DISCORD-EXPLICIT_MENTION-TASK_AGENT-AGENT_TO_OWNER` | discord | explicit_mention | task_agent | agent_to_owner | 適用 | owner_checklist | — |
| 6 | `ACR08-DISCORD-EXPLICIT_MENTION-TASK_AGENT-AGENT_TO_AGENT` | discord | explicit_mention | task_agent | agent_to_agent | 適用 | automated_dry_run | — |
| 7 | `ACR08-DISCORD-BOT_REPLY_ATTRIBUTED-NAGI-OWNER_TO_AGENT` | discord | bot_reply_attributed | nagi | owner_to_agent | 適用 | owner_checklist | — |
| 8 | `ACR08-DISCORD-BOT_REPLY_ATTRIBUTED-NAGI-AGENT_TO_OWNER` | discord | bot_reply_attributed | nagi | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 9 | `ACR08-DISCORD-BOT_REPLY_ATTRIBUTED-NAGI-AGENT_TO_AGENT` | discord | bot_reply_attributed | nagi | agent_to_agent | N/A | — | bot返信はチャネル着信の文脈であり内部通信に適用しない |
| 10 | `ACR08-DISCORD-BOT_REPLY_ATTRIBUTED-TASK_AGENT-OWNER_TO_AGENT` | discord | bot_reply_attributed | task_agent | owner_to_agent | 適用 | owner_checklist | — |
| 11 | `ACR08-DISCORD-BOT_REPLY_ATTRIBUTED-TASK_AGENT-AGENT_TO_OWNER` | discord | bot_reply_attributed | task_agent | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 12 | `ACR08-DISCORD-BOT_REPLY_ATTRIBUTED-TASK_AGENT-AGENT_TO_AGENT` | discord | bot_reply_attributed | task_agent | agent_to_agent | N/A | — | bot返信はチャネル着信の文脈であり内部通信に適用しない |
| 13 | `ACR08-DISCORD-BOT_REPLY_UNATTRIBUTED-NAGI-OWNER_TO_AGENT` | discord | bot_reply_unattributed | nagi | owner_to_agent | 適用 | owner_checklist | — |
| 14 | `ACR08-DISCORD-BOT_REPLY_UNATTRIBUTED-NAGI-AGENT_TO_OWNER` | discord | bot_reply_unattributed | nagi | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 15 | `ACR08-DISCORD-BOT_REPLY_UNATTRIBUTED-NAGI-AGENT_TO_AGENT` | discord | bot_reply_unattributed | nagi | agent_to_agent | N/A | — | bot返信はチャネル着信の文脈であり内部通信に適用しない |
| 16 | `ACR08-DISCORD-BOT_REPLY_UNATTRIBUTED-TASK_AGENT-OWNER_TO_AGENT` | discord | bot_reply_unattributed | task_agent | owner_to_agent | N/A | — | 帰属不能なbot返信はNagiへ集約される |
| 17 | `ACR08-DISCORD-BOT_REPLY_UNATTRIBUTED-TASK_AGENT-AGENT_TO_OWNER` | discord | bot_reply_unattributed | task_agent | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 18 | `ACR08-DISCORD-BOT_REPLY_UNATTRIBUTED-TASK_AGENT-AGENT_TO_AGENT` | discord | bot_reply_unattributed | task_agent | agent_to_agent | N/A | — | bot返信はチャネル着信の文脈であり内部通信に適用しない |
| 19 | `ACR08-DISCORD-THREAD_INHERITANCE-NAGI-OWNER_TO_AGENT` | discord | thread_inheritance | nagi | owner_to_agent | 適用 | owner_checklist | — |
| 20 | `ACR08-DISCORD-THREAD_INHERITANCE-NAGI-AGENT_TO_OWNER` | discord | thread_inheritance | nagi | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 21 | `ACR08-DISCORD-THREAD_INHERITANCE-NAGI-AGENT_TO_AGENT` | discord | thread_inheritance | nagi | agent_to_agent | N/A | — | スレッド継承はチャネル着信の文脈であり内部通信に適用しない |
| 22 | `ACR08-DISCORD-THREAD_INHERITANCE-TASK_AGENT-OWNER_TO_AGENT` | discord | thread_inheritance | task_agent | owner_to_agent | 適用 | owner_checklist | — |
| 23 | `ACR08-DISCORD-THREAD_INHERITANCE-TASK_AGENT-AGENT_TO_OWNER` | discord | thread_inheritance | task_agent | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 24 | `ACR08-DISCORD-THREAD_INHERITANCE-TASK_AGENT-AGENT_TO_AGENT` | discord | thread_inheritance | task_agent | agent_to_agent | N/A | — | スレッド継承はチャネル着信の文脈であり内部通信に適用しない |
| 25 | `ACR08-DISCORD-OBSERVATION_ONLY-NAGI-OWNER_TO_AGENT` | discord | observation_only | nagi | owner_to_agent | 適用 | owner_checklist | — |
| 26 | `ACR08-DISCORD-OBSERVATION_ONLY-NAGI-AGENT_TO_OWNER` | discord | observation_only | nagi | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 27 | `ACR08-DISCORD-OBSERVATION_ONLY-NAGI-AGENT_TO_AGENT` | discord | observation_only | nagi | agent_to_agent | 適用 | automated_dry_run | — |
| 28 | `ACR08-DISCORD-OBSERVATION_ONLY-TASK_AGENT-OWNER_TO_AGENT` | discord | observation_only | task_agent | owner_to_agent | N/A | — | 非合致は宛先を持たずタスク実行エージェント種別を適用できない |
| 29 | `ACR08-DISCORD-OBSERVATION_ONLY-TASK_AGENT-AGENT_TO_OWNER` | discord | observation_only | task_agent | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 30 | `ACR08-DISCORD-OBSERVATION_ONLY-TASK_AGENT-AGENT_TO_AGENT` | discord | observation_only | task_agent | agent_to_agent | N/A | — | Nagiラベルの負の制御1セルで代表する |
| 31 | `ACR08-SLACK-EXPLICIT_MENTION-NAGI-OWNER_TO_AGENT` | slack | explicit_mention | nagi | owner_to_agent | 適用 | owner_checklist | — |
| 32 | `ACR08-SLACK-EXPLICIT_MENTION-NAGI-AGENT_TO_OWNER` | slack | explicit_mention | nagi | agent_to_owner | 適用 | owner_checklist | — |
| 33 | `ACR08-SLACK-EXPLICIT_MENTION-NAGI-AGENT_TO_AGENT` | slack | explicit_mention | nagi | agent_to_agent | 適用 | automated_dry_run | — |
| 34 | `ACR08-SLACK-EXPLICIT_MENTION-TASK_AGENT-OWNER_TO_AGENT` | slack | explicit_mention | task_agent | owner_to_agent | 適用 | owner_checklist | — |
| 35 | `ACR08-SLACK-EXPLICIT_MENTION-TASK_AGENT-AGENT_TO_OWNER` | slack | explicit_mention | task_agent | agent_to_owner | 適用 | owner_checklist | — |
| 36 | `ACR08-SLACK-EXPLICIT_MENTION-TASK_AGENT-AGENT_TO_AGENT` | slack | explicit_mention | task_agent | agent_to_agent | 適用 | automated_dry_run | — |
| 37 | `ACR08-SLACK-BOT_REPLY_ATTRIBUTED-NAGI-OWNER_TO_AGENT` | slack | bot_reply_attributed | nagi | owner_to_agent | 適用 | owner_checklist | — |
| 38 | `ACR08-SLACK-BOT_REPLY_ATTRIBUTED-NAGI-AGENT_TO_OWNER` | slack | bot_reply_attributed | nagi | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 39 | `ACR08-SLACK-BOT_REPLY_ATTRIBUTED-NAGI-AGENT_TO_AGENT` | slack | bot_reply_attributed | nagi | agent_to_agent | N/A | — | bot返信はチャネル着信の文脈であり内部通信に適用しない |
| 40 | `ACR08-SLACK-BOT_REPLY_ATTRIBUTED-TASK_AGENT-OWNER_TO_AGENT` | slack | bot_reply_attributed | task_agent | owner_to_agent | 適用 | owner_checklist | — |
| 41 | `ACR08-SLACK-BOT_REPLY_ATTRIBUTED-TASK_AGENT-AGENT_TO_OWNER` | slack | bot_reply_attributed | task_agent | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 42 | `ACR08-SLACK-BOT_REPLY_ATTRIBUTED-TASK_AGENT-AGENT_TO_AGENT` | slack | bot_reply_attributed | task_agent | agent_to_agent | N/A | — | bot返信はチャネル着信の文脈であり内部通信に適用しない |
| 43 | `ACR08-SLACK-BOT_REPLY_UNATTRIBUTED-NAGI-OWNER_TO_AGENT` | slack | bot_reply_unattributed | nagi | owner_to_agent | 適用 | owner_checklist | — |
| 44 | `ACR08-SLACK-BOT_REPLY_UNATTRIBUTED-NAGI-AGENT_TO_OWNER` | slack | bot_reply_unattributed | nagi | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 45 | `ACR08-SLACK-BOT_REPLY_UNATTRIBUTED-NAGI-AGENT_TO_AGENT` | slack | bot_reply_unattributed | nagi | agent_to_agent | N/A | — | bot返信はチャネル着信の文脈であり内部通信に適用しない |
| 46 | `ACR08-SLACK-BOT_REPLY_UNATTRIBUTED-TASK_AGENT-OWNER_TO_AGENT` | slack | bot_reply_unattributed | task_agent | owner_to_agent | N/A | — | 帰属不能なbot返信はNagiへ集約される |
| 47 | `ACR08-SLACK-BOT_REPLY_UNATTRIBUTED-TASK_AGENT-AGENT_TO_OWNER` | slack | bot_reply_unattributed | task_agent | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 48 | `ACR08-SLACK-BOT_REPLY_UNATTRIBUTED-TASK_AGENT-AGENT_TO_AGENT` | slack | bot_reply_unattributed | task_agent | agent_to_agent | N/A | — | bot返信はチャネル着信の文脈であり内部通信に適用しない |
| 49 | `ACR08-SLACK-THREAD_INHERITANCE-NAGI-OWNER_TO_AGENT` | slack | thread_inheritance | nagi | owner_to_agent | 適用 | owner_checklist | — |
| 50 | `ACR08-SLACK-THREAD_INHERITANCE-NAGI-AGENT_TO_OWNER` | slack | thread_inheritance | nagi | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 51 | `ACR08-SLACK-THREAD_INHERITANCE-NAGI-AGENT_TO_AGENT` | slack | thread_inheritance | nagi | agent_to_agent | N/A | — | スレッド継承はチャネル着信の文脈であり内部通信に適用しない |
| 52 | `ACR08-SLACK-THREAD_INHERITANCE-TASK_AGENT-OWNER_TO_AGENT` | slack | thread_inheritance | task_agent | owner_to_agent | 適用 | owner_checklist | — |
| 53 | `ACR08-SLACK-THREAD_INHERITANCE-TASK_AGENT-AGENT_TO_OWNER` | slack | thread_inheritance | task_agent | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 54 | `ACR08-SLACK-THREAD_INHERITANCE-TASK_AGENT-AGENT_TO_AGENT` | slack | thread_inheritance | task_agent | agent_to_agent | N/A | — | スレッド継承はチャネル着信の文脈であり内部通信に適用しない |
| 55 | `ACR08-SLACK-OBSERVATION_ONLY-NAGI-OWNER_TO_AGENT` | slack | observation_only | nagi | owner_to_agent | 適用 | owner_checklist | — |
| 56 | `ACR08-SLACK-OBSERVATION_ONLY-NAGI-AGENT_TO_OWNER` | slack | observation_only | nagi | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 57 | `ACR08-SLACK-OBSERVATION_ONLY-NAGI-AGENT_TO_AGENT` | slack | observation_only | nagi | agent_to_agent | 適用 | automated_dry_run | — |
| 58 | `ACR08-SLACK-OBSERVATION_ONLY-TASK_AGENT-OWNER_TO_AGENT` | slack | observation_only | task_agent | owner_to_agent | N/A | — | 非合致は宛先を持たずタスク実行エージェント種別を適用できない |
| 59 | `ACR08-SLACK-OBSERVATION_ONLY-TASK_AGENT-AGENT_TO_OWNER` | slack | observation_only | task_agent | agent_to_owner | N/A | — | inbound専用、返信はdraft/approval契約で検証 |
| 60 | `ACR08-SLACK-OBSERVATION_ONLY-TASK_AGENT-AGENT_TO_AGENT` | slack | observation_only | task_agent | agent_to_agent | N/A | — | Nagiラベルの負の制御1セルで代表する |

## 検証観点

適用セルは配送先、Ledger Event、送り手/受け手個名、`WorkItem`/`Execution`参照、既存 `vsm audit-trace`、返信系の `reply-approval@1` ゲートを確認する。観測のみセルは観測が残り、`agent_notification_delivered` が存在しないことを確認する。

自動生成の正本は [vsm/acr08.py](../vsm/acr08.py) の `build_matrix()` であり、表の件数と適用判定を実行時にも再検証する。
