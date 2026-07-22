# ACR-02 通知配送基盤

ACR-02 の宛先付き着信と ACR-07 のエージェント間メッセージは、観測の保存とは別に同じ
個人 DataSpace の Operational Ledger へ `agent_notification_delivered` Event として記録する。
観測だけの着信は通知 Event を作らない。チャネル着信と ACR-07 のエージェント間通信は
同じ `AgentNotification` 契約と `AgentNotificationDelivery` を使用する。エージェント間の
送信は `POST /api/agent-messages` がこの delivery service を呼び、別の配送キューや
チャネル送信器を持たない。

## 二段構え

1. Intercom が宛先解決済みの着信について、観測を保存した後に
   `POST /api/operational-events` へ通知 Event を append する。
2. `requires_work_item = true` の通知だけが昇格条件を満たす。オーナーまたは認証済み
   制御面が、WorkItem の全フィールドを明示して
   `POST /api/notifications/{notification_id}/promotions` を実行する。
3. Kernel は既存の `create_work_item` 経路で WorkItem Event を記録し、その後
   `agent_notification_promoted` Event で通知と WorkItem を連結する。

条件を満たさない通知を暗黙に WorkItem 化したり、WorkItem の Node・route・acceptance を
推測したりしない。昇格要求がない通知は Ledger Event のまま滞留する。

## Ledger payload

`AgentNotification` は次を保持する。

- `recipient_agent_name`、`sender_agent_name`、`sender_actor_id`
- `source_platform`、`source_instance_id`、channel、message、source observation subject
- `resolution_kind`、`requires_work_item`、関連 WorkItem / Execution 参照
- `owner_visible = true`

## ACR-07 エージェント間通信

送り手・受け手は、`AgentNameRegistry` が発行した個名でなければならない。メッセージには
関連 WorkItem と Execution の両方を必須とし、Event の `correlation_id` と `causation_id`
および payload から後で復元できるようにする。オーナー向けの `GET /api/notifications`
と `GET /api/agent-messages` はエージェント間 Event を隠さず返す。

エージェント名は WorkItem dispatch 外からも `POST /api/agent-identities` で取得できる。
この API は設定済み CSV レジストリにだけ割当を依頼し、割当結果を
`agent_identity_registered` Event として Ledger に記録する。dispatch の割当と同じ名前空間
を共有するため、レジストリ外の手作業名が既存名と衝突する経路は受け付けない。

エージェント間の source platform は `internal` に固定され、`reply-approval@1` や
card-queue を経由しない。外部チャネルへの発信は既存の `reply-draft@1` →
`reply-approval@1` → `lethe-channel-bridge` 経路だけが担う。

Intercom は Operational Ledger の `space:personal-primary` に対して
`write:operational` が許可された LETHE write token を必要とする。既存の observation
import は引き続き同じ着信について実行され、Ledger Event の idempotency key は通知内容から
決定的に生成される。

## API

- `GET /api/notifications`: オーナー可視の通知 projection
- `POST /api/notifications`: Nanihold 内部または認証済み接続部から通知を append
- `POST /api/notifications/{id}/promotions`: 明示 WorkItem を通知から起票

承認レスや外部チャネル送信はこの基盤に含めない。外部返信は既存の
`reply-draft@1` → `reply-approval@1` → Intercom card-queue 経路を使用する。
