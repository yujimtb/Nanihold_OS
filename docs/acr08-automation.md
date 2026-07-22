# ACR-08 自動化セルの疎通スクリプト

自動化対象は6セル（Discord/Slack × explicit internal messageのNagi/タスク実行エージェント、各チャネルの観測のみ負の制御）である。スクリプトはACR-02とACR-07の共有 `POST /api/agent-messages` 契約を計画化し、観測のみのセルはLedger読取で誤配送ゼロを検証する。

## dry-run

```powershell
python scripts/acr08_connectivity.py dry-run --output acr08-dry-run.json
```

dry-runの出力には6セル分のHTTPメソッド・パス・body・期待結果が含まれる。`dry_run: true`、`real_discord_or_slack_send: false` を必須値とし、Discord/Slack adapter、channel credential、外部送信経路は持たない。実行時に未指定の個名・WorkItem・Executionは `<...>` として明示され、silent fallbackしない。

## 自動化セルの運用契約

1. `POST /api/agent-messages` を使う場合は、実行エージェントのRegistry発行個名2つ、関連WorkItem、Executionを明示する。
2. 応答後、`GET /api/events` と `GET /api/agent-messages` を読み、`agent_notification_delivered` のEvent ID、cursor、sender/recipient、WorkItem/Execution参照を保存する。
3. 観測のみの負の制御では、対象Observation subjectが残り、同subjectの `agent_notification_delivered` が存在しないことを保存する。
4. `scripts/acr08_connectivity.py verify` が既存 `vsm audit-trace` 形式の `verified: true` とLedger Eventを突合する。検証はread-onlyであり、Eventを追加しない。
5. `agent_to_agent`方向（内部経路）セルは実チャネルを経由しないため、監査証跡の `incoming.source_platform` は常に `"internal"` になる（`AgentNotificationDelivery.send_agent_message` が常にこの値で記録するため）。検証器はこのセルに限り `source_platform` として `None` / チャネル名 / `"internal"` を許容し、`owner_to_agent` など実チャネル着信セルでは従来どおり `None` / チャネル名のみを許容する。

実Discord/実Slackへの送信、承認レスの自動送信、実LETHEへの実測はこのWorkItemでは行わない。オーナー承認後の実行時にだけ、同じdry-run計画へ実値を注入して別途行う。
