# Nanihold public API

すべての REST request は `Authorization: Bearer <token>` を要求します。CORS origin は設定で明示します。未定義 field は拒否します。

## Resource endpoints

| Method | Path | 契約 |
|---|---|---|
| GET | `/api/data-spaces` | 現在の DataSpace |
| GET/POST | `/api/nodes` | Node Tree、CapabilityGrant、ReferenceGrant |
| GET/POST | `/api/work-items` | WorkItem と Work Graph |
| POST | `/api/work-items/{id}/interventions` | 対象WorkItem、Execution、Effectだけを停止 |
| GET/POST | `/api/executions` | Execution、Effect Lease、BudgetReservation |
| GET | `/api/events?after_cursor=&limit=` | cursor 付き canonical Event page |
| GET/POST | `/api/conversations` | 認証済みowner向け会話表示、決定、約束、Node memory |
| GET | `/api/conversations/{id}` | model-free status |
| POST | `/api/conversations/{id}/messages` | owner message を先に保存し、Interface Pilot を最大一回呼ぶ |
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

RouteSnapshot の `evidence_cursor` は登録時点の verified outcome cursor と完全一致しなければなりません。candidate key は model 名ではなく `adapter@version + provider/model snapshot + effort + toolset + sandbox + environment` の canonical hash です。

owner messageへの応答は表示、Work directive、decision、commitment update、provider session IDに加え、`pilot_usage`を必須とします。usageにはcandidate key、actual provider/model、input、cache creation input、cache read input、output token、USD費用、durationを含めます。同じ内容を`interface_response_recorded` Eventにも保存し、WebUIは直近turnの実測値を表示します。

## PilotHost stream

`/api/pilot-hosts/{pilot_host_id}/stream` は Bearer 認証付き WebSocket です。

1. client は `identity`、`acknowledged_cursor`、`connected_at` を送る。
2. server は cursor より後の Event page を送る。
3. client は `ack` または `tail` だけを送る。
4. 切断時はその PilotHost に属する active Execution を pause する。

## Interface PilotHost RPC

Interfaceの実provider呼出しはNanihold process外のPilotHostが所有します。これは公開RESTではなく、Bearer認証されたdevice境界です。

- `GET /health`: exact candidate key、model snapshot、effort、tool状態を返す
- `POST /v1/interface-turn`: exact candidate、owner text、resumeまたはdelta contextだけを受ける
- 応答: requested candidate key、actual provider/model、structured response、provider session、実測usage

起動時のhealth candidateまたはturn時のactual modelが設定と違えばHTTP 409として応答を破棄します。model aliasからactual snapshotを推定せず、provider CLIが返した利用modelだけを採用します。

## Error semantics

- 認証不備: HTTP 401
- 不変条件、stream conflict、model mismatch: HTTP 409
- 未定義 route: HTTP 404
- malformed payload: HTTP 422

別 mode、別 backend、別 model への fallback はしません。
