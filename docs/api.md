# Nanihold public API

IntercomとPilotHostは`Authorization: Bearer <token>`と登録済み
`X-Nanihold-Device-Id`を共に要求します。Web ownerは一回限りbootstrap codeを
`POST /api/owner-bootstrap/exchange`へ渡し、HttpOnly・SameSite=Strict cookieを得ます。
codeは短寿命で再利用不可、Ledgerにはhashだけを保存します。CORS origin、method、headerは
明示列挙し、credentialを許可します。未定義fieldは拒否します。

## Resource endpoints

| Method | Path | 契約 |
|---|---|---|
| GET | `/api/data-spaces` | 現在の DataSpace |
| GET/POST | `/api/nodes` | Node Tree、CapabilityGrant、ReferenceGrant |
| GET/POST | `/api/work-items` | WorkItem と Work Graph |
| POST | `/api/work-items/{id}/delegations` | delegated Nodeを確定 |
| POST | `/api/work-items/{id}/interventions` | 対象WorkItem、Execution、Effectだけを停止 |
| GET/POST | `/api/executions` | Execution、Effect Lease、BudgetReservation |
| POST | `/api/effects/{id}/approval` | planned Effectをowner承認 |
| GET | `/api/events?after_cursor=&limit=` | cursor 付き canonical Event page |
| GET/POST | `/api/conversations` | canonical ConversationとSurfaceBinding |
| GET | `/api/conversations/{id}` | model-free status |
| POST | `/api/conversations/{id}/actions` | `OwnerMessageAction`を受付（202） |
| GET | `/api/conversations/{id}/actions/{action_id}` | transport不明時のreceipt照合 |
| GET/POST | `/api/history/imports` | current Work GraphとLETHE activation handoffの厳密な取込gate |
| GET | `/api/history/sessions` | model-free履歴session索引 |
| GET/POST | `/api/reorientation` | Assessment表示／提出 |
| POST | `/api/reorientation/start` | 履歴照会専用mode開始 |
| POST | `/api/reorientation/revision` | owner/systemの理由コードで不備Assessmentを再評価へ戻す |
| POST | `/api/reorientation/queries` | size制限付きLETHE HistoryReader照会 |
| POST | `/api/reorientation/approval` | owner訂正記録と初回activation |
| GET | `/api/activation/status` | model-free起動状態と累積reorientation usage |
| GET | `/api/pilot-hosts` | device と接続状態 |
| POST | `/api/pilot-hosts/connect` | identity と cursor で接続 |
| POST | `/api/pilot-hosts/{id}/disconnect` | 対象 Execution を pause |
| GET | `/api/model-registry` | exact candidate、verified outcome、evidence cursor |
| POST | `/api/model-registry/outcomes` | 検証済み outcome を Event 化 |
| GET/POST | `/api/route-snapshots` | route score と snapshot 登録 |
| POST | `/api/route-snapshots/{id}/approvals` | `s3_star` の後に `owner` |
| POST | `/api/route-snapshots/{id}/publish` | owner-approved snapshot を公開 |
| GET | `/api/token-lab` | baseline と observation |
| POST | `/api/token-lab/baselines` | 承認済み baseline を Event 化 |
| POST | `/api/token-lab/observations` | 観測を保存してロジック判定 |
| POST | `/api/token-lab/weekly-reviews` | model-free週次判定の完了をEvent化 |

POST command は resource payload に加えて `actor_id` と `idempotency_key` を要求します。会話作成と owner message は owner identity が resource から確定するため専用 request 型です。

`GET /api/activation/status`はmodelを呼ばず、Assessmentと累積usageに加えて
`reorientation_attempt_in_progress`と`pending_reorientation_revision_reason`を返します。
attempt中の別idempotency keyによる二重startも拒否します。runtime restartでbackground
attemptが失われた場合はsilent resetせず、固定理由コードと安定idempotency keyを持つ
`reorientation_attempt_interrupted` Eventを記録してから明示retry可能にします。

`POST /api/history/imports`はLETHEの
`schema:history-activation-handoff` `1.0.0` receiptと
`CurrentWorkGraphSnapshot`、既存canonical Conversationの
`reorientation_conversation_id`を同時に要求します。Conversationは対象DataSpaceと
ownerに一致していなければなりません。Work Graphを先に永続化し、
同じrequest内のhandoff検証へ進みます。7 source kind、DataSpace、件数、bytes、
digest、cursor、session provenance、canonical Conversationの不一致は409/422で停止します。
このConversation IDはactivation Eventへ永続化され、Interface PilotのAssessmentは同じIDを
参照しなければ受理されません。owner approvalもAssessmentと同じConversation IDを
要求し、別surfaceへの訂正・承認の記録を拒否します。

model-free probeは認証を要求しません。

| Method | Path | 契約 |
|---|---|---|
| GET | `/health/live` | process liveness、`model_calls=0` |
| GET | `/health/ready` | Event Ledger read可否とactivation state、`model_calls=0` |

RouteSnapshot の `evidence_cursor` は登録時点の verified outcome cursor と完全一致しなければなりません。candidate key は `adapter@version + provider + selection + effort + toolset + sandbox + environment` の canonical hash です。`selection`はcoding用のexact model snapshotまたはInterface用の`provider_configured`であり、Interfaceの人格名や暫定モデル名を含めません。

Conversation作成requestは`conversation`、`surface_binding`、`idempotency_key`のexact schemaです。
owner入力はsource surface/session/message/author/channel/timeを含む`OwnerMessageAction`です。
同じaction IDとpayloadは同じreceiptを返し、内容が違えばcollisionとして拒否します。

Interface応答は表示、型付き`InterfaceAction`、provider session ID、`pilot_usage`を必須とします。
usageにはcandidate、actual provider/model、input/cache/output token、費用、durationに加え、
classifier、model substitution、full-history resend、polling、false-complete、再編集tokenを含めます。
TokenObservationは応答Eventから自動生成し、手動POSTを通常経路にしません。

## PilotHost stream

`/api/pilot-hosts/{pilot_host_id}/stream` はBearer、device ID、許可Origin付き
Kernel control WebSocketです。

1. client は `identity`、`acknowledged_cursor`、`connected_at` を送る。
2. server は cursor より後の Event page を送る。
3. client は `ack` または `tail` だけを送る。
4. 切断時はその PilotHost に属する active Execution を pause する。

現行production PilotHost processは下記HTTP RPCを実装しており、このWebSocketへ
常駐接続する外向きresumable transportは未実装です。HA cutoverはこの差を
runtime capability receiptでfail-fastします。

## Interface PilotHost RPC

Interfaceの実provider呼出しはNanihold process外のPilotHostが所有します。これは公開RESTではなく、Bearer認証されたdevice境界です。

- `GET /health`: candidate key、selection、exact selectionの場合だけmodel snapshot、effort、tool状態を返す
- `POST /v1/interface-turn`: exact candidate、owner text、resumeまたはdelta contextだけを受ける
- 応答: requested candidate key、actual provider/model、structured response、provider session、実測usage

起動時のhealth candidateが設定と違えばHTTP 409として応答を破棄します。exact selectionではturn時のactual model不一致も拒否します。`provider_configured`のInterface Pilotは固有modelを要求せず、provider CLIが返したactual modelをreceiptへ記録します。model aliasからactual snapshotを推定しません。

## Error semantics

- 認証不備: HTTP 401
- 不変条件、stream conflict、model mismatch: HTTP 409
- 未定義 route: HTTP 404
- malformed payload: HTTP 422

旧`/api/chat*`、`/api/runs*`、`/api/conversations/{id}/messages`は存在しません。

別 mode、別 backend、別 model への fallback はしません。
