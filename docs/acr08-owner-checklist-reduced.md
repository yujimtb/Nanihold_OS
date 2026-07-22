# ACR-08 オーナー実施手順チェックリスト(縮約版)

対象は実Discord/実Slackの画面から行う**12項目**。全20セルの完全版(`acr08-owner-checklist.md`)は等価シミュレーションにより内部経路が20/20検証済みのため、実画面テストは「Gateway境界(実チャネル→宛先解決→通知)」と「実外部送信(承認→実送信)」だけをカバーすればよいとオーナーが決定した。

## 縮約の根拠

`intercom/addressing.py` の `AddressResolver.resolve()` は宛先が Nagi か割当済みタスクエージェント個名かによって分岐しない。`_explicit_recipient` は正規表現でマッチした個名文字列をそのまま `recipient` に入れるだけであり、`_bot_attribution` も `parent.agent_name or parent.recipient` を素通しするだけで、個名の種類(Nagi固定席かタスクエージェントか)で処理経路が変わる箇所はない。つまり **Gateway境界においてNagi宛てとタスクエージェント宛ては同一処理** であり、片方(Nagi)を実画面で通せば、もう片方は構造的に等価と言える。実際の等価性はシミュレーションで20/20検証済みなので、実画面ではNagi代表の1系統だけを流す。

## 代表セルID対応表

`#` は本書の項目番号。「実施」列が本書で実際に画面から流すセルID、「sim検証済み」列は上記の等価性によりカバーされる関連セルID(等価シミュレーションで検証済み、実画面では実施しない)。

| # | チャネル | 縮約項目 | 実施(実画面) Cell ID | sim検証済み(等価/内部経路) Cell ID |
|---:|---|---|---|---|
| 1 | discord | 文頭@メンション | `ACR08-DISCORD-EXPLICIT_MENTION-NAGI-OWNER_TO_AGENT` | `ACR08-DISCORD-EXPLICIT_MENTION-TASK_AGENT-OWNER_TO_AGENT` |
| 2 | discord | bot返信・帰属あり | `ACR08-DISCORD-BOT_REPLY_ATTRIBUTED-NAGI-OWNER_TO_AGENT` | `ACR08-DISCORD-BOT_REPLY_ATTRIBUTED-TASK_AGENT-OWNER_TO_AGENT` |
| 3 | discord | bot返信・帰属なし→Nagi集約 | `ACR08-DISCORD-BOT_REPLY_UNATTRIBUTED-NAGI-OWNER_TO_AGENT` | (task_agent側は元々N/A。帰属不能はNagiへ集約されるため対になるセルが存在しない) |
| 4 | discord | スレッド継承 | `ACR08-DISCORD-THREAD_INHERITANCE-NAGI-OWNER_TO_AGENT` | `ACR08-DISCORD-THREAD_INHERITANCE-TASK_AGENT-OWNER_TO_AGENT` |
| 5 | discord | 非合致→観測のみ | `ACR08-DISCORD-OBSERVATION_ONLY-NAGI-OWNER_TO_AGENT` | `ACR08-DISCORD-OBSERVATION_ONLY-NAGI-AGENT_TO_AGENT`(task_agent側は非合致のため元々N/A) |
| 6 | slack | 文頭@メンション | `ACR08-SLACK-EXPLICIT_MENTION-NAGI-OWNER_TO_AGENT` | `ACR08-SLACK-EXPLICIT_MENTION-TASK_AGENT-OWNER_TO_AGENT` |
| 7 | slack | bot返信・帰属あり | `ACR08-SLACK-BOT_REPLY_ATTRIBUTED-NAGI-OWNER_TO_AGENT` | `ACR08-SLACK-BOT_REPLY_ATTRIBUTED-TASK_AGENT-OWNER_TO_AGENT` |
| 8 | slack | bot返信・帰属なし→Nagi集約 | `ACR08-SLACK-BOT_REPLY_UNATTRIBUTED-NAGI-OWNER_TO_AGENT` | (同上、対になるセルなし) |
| 9 | slack | スレッド継承 | `ACR08-SLACK-THREAD_INHERITANCE-NAGI-OWNER_TO_AGENT` | `ACR08-SLACK-THREAD_INHERITANCE-TASK_AGENT-OWNER_TO_AGENT` |
| 10 | slack | 非合致→観測のみ | `ACR08-SLACK-OBSERVATION_ONLY-NAGI-OWNER_TO_AGENT` | `ACR08-SLACK-OBSERVATION_ONLY-NAGI-AGENT_TO_AGENT`(task_agent側は非合致のため元々N/A) |
| 11 | discord | 返信承認→実外部送信 | `ACR08-DISCORD-EXPLICIT_MENTION-NAGI-AGENT_TO_OWNER` | `ACR08-DISCORD-EXPLICIT_MENTION-TASK_AGENT-AGENT_TO_OWNER` |
| 12 | slack | 返信承認→実外部送信 | `ACR08-SLACK-EXPLICIT_MENTION-NAGI-AGENT_TO_OWNER` | `ACR08-SLACK-EXPLICIT_MENTION-TASK_AGENT-AGENT_TO_OWNER` |

この12項目の実施で、完全版20セル(owner_to_agent 16 + agent_to_owner 4)全てが「実画面 or 等価sim」のいずれかでカバーされる。

## 実環境

- Discord: チャネル `general`(channel_id `1509933604820746425`、guild `1509933604820746422`)
- Slack: チャネル `C0BH1P6MXJ8`
- 宛先個名: `@Nagi`(レジストリ予約席。今回はNagi代表のみで、タスクエージェント個名への置換は不要)
- 宛先記法の先頭文字は `@`。正規表現は「`@`+個名+空白 or 行末」に一致する必要があるため、**`@Nagi` の直後に半角スペースを必ず入れる**こと(詰めて書くとexplicit_mentionとして認識されない)。

## ■Nagi側準備(オーナー操作不要・私が事前に実施)

以下はオーナーの操作前に完了させておく。完了したら、投稿したメッセージへの直リンクをオーナーへ共有する。

- **discord-bot帰属あり**: Discord `general` へ、Nagi個名帰属付きのbotメッセージを投稿(項目2で返信する対象)
- **discord-bot帰属なし**: Discord `general` へ、帰属メタデータなしのraw botメッセージを投稿(項目3で返信する対象)
- **slack-bot帰属あり**: Slack `C0BH1P6MXJ8` へ、Nagi個名帰属付きのbotメッセージを投稿(項目7で返信する対象)
- **slack-bot帰属なし**: Slack `C0BH1P6MXJ8` へ、帰属メタデータなしのraw botメッセージを投稿(項目8で返信する対象)
- **discord-返信ドラフト**: Discord向け返信ドラフトを起草し、card-queueへ投入(項目11でオーナーが承認する対象)
- **slack-返信ドラフト**: Slack向け返信ドラフトを起草し、card-queueへ投入(項目12でオーナーが承認する対象)

## 本体チェックリスト

### 項目1 discord — 文頭@メンション

- [ ] **チャネル**: Discord `general`
- **■Nagi側準備**: なし
- **■オーナー操作**: 下記をチャネルへそのまま新規投稿する(このメッセージは項目4のスレッド親を兼ねる)。

```
@Nagi ACR08 discord explicit_mention nagi
```

- **期待結果**: Nagi宛の通知が発生する。Ledger Eventが記録され、監査トレースが `verified: true` になる。
- **備考**: Cell ID `ACR08-DISCORD-EXPLICIT_MENTION-NAGI-OWNER_TO_AGENT`。この投稿のmessage_idが項目4のスレッド親になるため、URLかIDを控えておくと項目4がスムーズ。

### 項目2 discord — bot返信・帰属あり

- [ ] **チャネル**: Discord `general`
- **■Nagi側準備**: 帰属あり botメッセージをNagiが事前投稿済み(上記「■Nagi側準備」参照)。共有されたリンク先のメッセージを使う。
- **■オーナー操作**: Nagiが投稿した帰属ありbotメッセージに対し、Discordの「返信(Reply)」機能で以下を送信する。

```
ACR08 discord bot_reply_attributed nagi bot attribution reply
```

- **期待結果**: bot帰属(Nagi)が保持されたままNagiへ通知される。Ledger Event記録、監査トレース `verified: true`。
- **備考**: Cell ID `ACR08-DISCORD-BOT_REPLY_ATTRIBUTED-NAGI-OWNER_TO_AGENT`。文頭に `@Nagi` は付けない(付けるとexplicit_mentionとして扱われ本セルの検証にならない)。必ずDiscordの返信機能(reply_to_message_id が付く形)を使うこと。

### 項目3 discord — bot返信・帰属なし → Nagi集約

- [ ] **チャネル**: Discord `general`
- **■Nagi側準備**: 帰属なし(raw)botメッセージをNagiが事前投稿済み。共有されたリンク先のメッセージを使う。
- **■オーナー操作**: Nagiが投稿した帰属なしbotメッセージに対し、Discordの「返信(Reply)」機能で以下を送信する。

```
ACR08 discord bot_reply_unattributed nagi unattributed bot reply
```

- **期待結果**: 観測落ちせず、Nagiへ集約されて通知される(帰属情報がないため `addressing.py` のフォールバックでNagi宛てになる)。Ledger Event記録、監査トレース `verified: true`。
- **備考**: Cell ID `ACR08-DISCORD-BOT_REPLY_UNATTRIBUTED-NAGI-OWNER_TO_AGENT`。文頭に `@Nagi` は付けない。

### 項目4 discord — スレッド継承

- [ ] **チャネル**: Discord `general`
- **■Nagi側準備**: なし(項目1の投稿がGatewayで処理され、Nagi宛てとして記録されるのを数秒待つ)
- **■オーナー操作**: 項目1で投稿したメッセージに対し、Discordの「返信(Reply)」機能で、**@メンションを付けずに**以下を送信する。

```
ACR08 discord thread_inheritance nagi inherited thread reply
```

- **期待結果**: 親(項目1)のNagi宛先を継承して通知される。Ledger Event記録、監査トレース `verified: true`。
- **備考**: Cell ID `ACR08-DISCORD-THREAD_INHERITANCE-NAGI-OWNER_TO_AGENT`。項目1の直後すぐに送ると親メッセージがまだ記録前で継承に失敗する可能性があるため、数秒待ってから送信する。

### 項目5 discord — 非合致 → 観測のみ

- [ ] **チャネル**: Discord `general`
- **■Nagi側準備**: なし
- **■オーナー操作**: @メンションなし・どの投稿への返信でもない、通常の新規メッセージとして以下を送信する。

```
ACR08 discord observation_only nagi observation-only message
```

- **期待結果**: LETHEに観測のみ記録される。`agent_notification_delivered` は発生しない。誤って誰かに通知が飛ばないこと。
- **備考**: Cell ID `ACR08-DISCORD-OBSERVATION_ONLY-NAGI-OWNER_TO_AGENT`。「何も起きないこと」を確認する項目。

### 項目6 slack — 文頭@メンション

- [ ] **チャネル**: Slack `C0BH1P6MXJ8`
- **■Nagi側準備**: なし
- **■オーナー操作**: 下記をチャネルへそのまま新規投稿する(このメッセージは項目9のスレッド親を兼ねる)。

```
@Nagi ACR08 slack explicit_mention nagi
```

- **期待結果**: Nagi宛の通知が発生する。Ledger Event記録、監査トレース `verified: true`。
- **備考**: Cell ID `ACR08-SLACK-EXPLICIT_MENTION-NAGI-OWNER_TO_AGENT`。この投稿のtsが項目9のスレッド親になるため控えておく。

### 項目7 slack — bot返信・帰属あり

- [ ] **チャネル**: Slack `C0BH1P6MXJ8`
- **■Nagi側準備**: 帰属あり botメッセージをNagiが事前投稿済み。共有されたリンク先のメッセージを使う。
- **■オーナー操作**: Nagiが投稿した帰属ありbotメッセージに対し、Slackの「スレッドで返信(Reply in thread)」で以下を送信する。

```
ACR08 slack bot_reply_attributed nagi bot attribution reply
```

- **期待結果**: bot帰属(Nagi)が保持されたままNagiへ通知される。Ledger Event記録、監査トレース `verified: true`。
- **備考**: Cell ID `ACR08-SLACK-BOT_REPLY_ATTRIBUTED-NAGI-OWNER_TO_AGENT`。文頭に `@Nagi` は付けない。チャネルへの直返信ではなく必ず「スレッドで返信」を使うこと(Slackは `thread_ts` でしか返信を判定しない)。

### 項目8 slack — bot返信・帰属なし → Nagi集約

- [ ] **チャネル**: Slack `C0BH1P6MXJ8`
- **■Nagi側準備**: 帰属なし(raw)botメッセージをNagiが事前投稿済み。共有されたリンク先のメッセージを使う。
- **■オーナー操作**: Nagiが投稿した帰属なしbotメッセージに対し、Slackの「スレッドで返信(Reply in thread)」で以下を送信する。

```
ACR08 slack bot_reply_unattributed nagi unattributed bot reply
```

- **期待結果**: 観測落ちせず、Nagiへ集約されて通知される。Ledger Event記録、監査トレース `verified: true`。
- **備考**: Cell ID `ACR08-SLACK-BOT_REPLY_UNATTRIBUTED-NAGI-OWNER_TO_AGENT`。文頭に `@Nagi` は付けない。

### 項目9 slack — スレッド継承

- [ ] **チャネル**: Slack `C0BH1P6MXJ8`
- **■Nagi側準備**: なし(項目6の投稿がGatewayで処理され、Nagi宛てとして記録されるのを数秒待つ)
- **■オーナー操作**: 項目6で投稿したメッセージに対し、Slackの「スレッドで返信(Reply in thread)」で、**@メンションを付けずに**以下を送信する。

```
ACR08 slack thread_inheritance nagi inherited thread reply
```

- **期待結果**: 親(項目6)のNagi宛先を継承して通知される。Ledger Event記録、監査トレース `verified: true`。
- **備考**: Cell ID `ACR08-SLACK-THREAD_INHERITANCE-NAGI-OWNER_TO_AGENT`。項目6の直後すぐに送ると継承に失敗する可能性があるため、数秒待ってから送信する。

### 項目10 slack — 非合致 → 観測のみ

- [ ] **チャネル**: Slack `C0BH1P6MXJ8`
- **■Nagi側準備**: なし
- **■オーナー操作**: @メンションなし・どのスレッドにも属さない、通常の新規メッセージとして以下を送信する。

```
ACR08 slack observation_only nagi observation-only message
```

- **期待結果**: LETHEに観測のみ記録される。`agent_notification_delivered` は発生しない。誤って誰かに通知が飛ばないこと。
- **備考**: Cell ID `ACR08-SLACK-OBSERVATION_ONLY-NAGI-OWNER_TO_AGENT`。「何も起きないこと」を確認する項目。

### 項目11 discord — 返信承認 → 実外部送信

- [ ] **チャネル**: Discord `general`
- **■Nagi側準備**: Nagiが起草した返信ドラフト(本文 `ACR08 discord explicit_mention nagi reply-draft body`)をcard-queueへ投入済み。
- **■オーナー操作**: card-queueを開き、該当ドラフトを確認のうえ承認する。承認操作自体が「送る文言」であり、追加のチャット入力は不要。
- **期待結果**: 承認後、botがDiscordの `general` チャネルへ実際にメッセージを送信する(画面上で実際に投稿が現れる)。`draft → reply-approval@1 → 既存bridgeのsend-record` の流れが成立し、承認前は送信0件であること。
- **備考**: Cell ID `ACR08-DISCORD-EXPLICIT_MENTION-NAGI-AGENT_TO_OWNER`。ここは唯一「実際にチャネルへメッセージが送られる」項目。送信時刻をメモしておくと後の照合が早い。

### 項目12 slack — 返信承認 → 実外部送信

- [ ] **チャネル**: Slack `C0BH1P6MXJ8`
- **■Nagi側準備**: Nagiが起草した返信ドラフト(本文 `ACR08 slack explicit_mention nagi reply-draft body`)をcard-queueへ投入済み。
- **■オーナー操作**: card-queueを開き、該当ドラフトを確認のうえ承認する。
- **期待結果**: 承認後、botがSlackの `C0BH1P6MXJ8` チャネルへ実際にメッセージを送信する(画面上で実際に投稿が現れる)。`draft → reply-approval@1 → 既存bridgeのsend-record` の流れが成立し、承認前は送信0件であること。
- **備考**: Cell ID `ACR08-SLACK-EXPLICIT_MENTION-NAGI-AGENT_TO_OWNER`。送信時刻をメモしておくと後の照合が早い。

## 実施順の推奨

1. まずNagi(私)が「■Nagi側準備」の6件(discord/slack各3件: bot帰属あり投稿・bot帰属なし投稿・返信ドラフト投入)を全て完了させ、必要なリンクをオーナーへ共有する。
2. オーナーは項目1→2→3→4→5(Discord)、続いて項目6→7→8→9→10(Slack)の順に実施する。項目1と6はそれぞれ項目4・9のスレッド親を兼ねるため、この順序を保つこと。
3. 最後に項目11(Discord)→項目12(Slack)で返信承認を実施する。これが唯一の実チャネルへの実送信であり、他の10項目より先に行うと承認対象のドラフトがまだ育っていない場合があるため、必ず最後に回す。
4. 所要時間の目安は準備を除き15〜20分。

## 完了後

実施完了の合図をNagiに伝えれば、検証(Ledger Event・監査トレースとの照合)はシステム側で自動的に実施される。オーナーが結果を記録する必要はない。送信時刻(特に項目11・12)だけメモしておくことを推奨する。
