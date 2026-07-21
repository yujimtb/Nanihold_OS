# Operations

## 1. 必須設定

`vsm.toml` は repository へ commit せず、`config/nanihold.example.toml` を基に環境ごとに作成します。次はすべて必須です。

- LETHE base URL、Bearer token 環境変数、DataSpace ID、Lake location
- AuditPolicy、ControlPolicy
- Pilot mode、PilotHost device identity
- `sandboxed_bypass` の SandboxProfile certificate と digest
- Interface Pilot はprovider設定のClaude modelを使い、effortだけを`high`に固定する。
- candidate ごとの versioned benchmark prior
- active RouteSnapshot ID
- API Bearer token、許可device ID、owner session lifetime、CORS origin
- LETHE HistoryReaderの`history_max_result_bytes`

example の `EXAMPLE-REPLACE` は構造説明用であり production 証拠ではありません。

実providerまでのローカル確認はproduction設定を流用せず、専用の
`deployment.mode=local_verification`を使います。初期化、起動、停止、費用上限は
[local-verification.md](local-verification.md)に記載します。

## 2. Claude Pilot mode

| mode | 書込 | classifier | 開始条件 |
|---|---:|---:|---|
| `sandboxed_bypass` | 可 | 0 | 有効な SandboxProfile 証明、classifier disabled |
| `managed_permissions` | policy 次第 | 使用 | classifier enabled。拒否数と再編集 token を計測 |
| `observe_only` | 不可 | mode 次第 | write Effect capability を拒否 |

条件不足時は開始しません。別 mode へ切り替えません。Claude CLI の `--model`、`--effort`、`--resume`、permission 処理は Adapter 内だけにあります。

Interface Adapterは認証済みPilotHostへRPCし、起動時にcandidate keyと
`model_selection=provider_configured`を照合します。固有modelは要求せず、
Claude Codeの`modelUsage`が示すactual modelをreceiptへ記録します。
top-level表示名やaliasから実モデルを推定しません。codingのexact selectionでは
requested snapshotとactual modelを照合します。

## 3. Route commissioning

1. `vsm routes models --config vsm.toml` で exact key を表示する。この操作は LETHE やモデルを呼ばない。
2. public prior の出典、版、sample 数、harness を確認する。
3. S4 sandbox の deterministic gate、人間判定、必要なら安価な `low` Judge から verified outcome を記録する。
4. `reliability_then_cost`、`expected_utility`、`quality_max` の全 score を確認する。
5. 現在の evidence cursor で RouteSnapshot を登録する。
6. 独立 S3*、owner の順に承認して公開する。
7. `routing.active_route_snapshot_id` と公開 snapshot を一致させる。

実行中Projectionはモデルを呼ばずにCLIから確認できます。

```powershell
vsm inspect nodes \
  --base-url http://localhost:8000 \
  --bearer-token-env NANIHOLD_API_BEARER_TOKEN \
  --device-id device:operator
vsm inspect events \
  --base-url http://localhost:8000 \
  --bearer-token-env NANIHOLD_API_BEARER_TOKEN \
  --device-id device:operator \
  --after-cursor 0 \
  --limit 250
```

resource は `data-spaces`、`nodes`、`work-items`、`executions`、`events`、`notifications`、
`agent-messages`、`agent-identities`、`conversations`、`pilot-hosts`、`model-registry`、
`route-snapshots`、`token-lab` です。`agent-messages` は `notifications` と同じ
Operational Ledger projection であり、外部チャネル送信のキューではありません。

単一Executionの障害調査では、Ledgerを手動joinせずにdispatch、receipt記録、receiptの
`usage`・`actual_model`・`error`、`provider_session_id`参照をcursor順で確認します。
Executionが存在しない場合はreceipt照会を続けずfail-fastします。
出力の`timeline`はLedgerイベントを含み、receiptイベントにはreceipt本体、全体には最新の
Execution状態と`provider_session_id_refs`を含めます。

```powershell
vsm trace execution:example --config vsm.toml
```

ACR-04 の配送・帰属トレースは次で確認します。通知とExecutionの読取はOperational
Ledgerだけで完結し、返信だけはLETHEから取得したsupplemental envelopeのJSON配列を
明示的に渡します。トレース中にEventや補助記録を作成・推測することはありません。

```powershell
vsm audit-trace notification:example --config vsm.toml
vsm audit-trace execution:example --config vsm.toml
vsm audit-trace sup:draft-example --config vsm.toml --supplementals supplementals.json
```

production objective は `quality_max` です。production exploration は禁止です。証拠更新後は古い snapshot が stale になり、再起動時に失敗します。

## 4. 初回履歴取込とowner activation

cutover用`runtime.env`はLETHE専用tokenを複製し、Nanihold APIとPilotHostには
それぞれ別の新規secretを発行する。値を端末へ表示しない。

```powershell
.\scripts\create_activation_runtime_env.ps1 `
  -LetheEnvFile D:\secure\personal-lake\.env `
  -OutputFile D:\secure\interface-activation\runtime.env
```

出力先が存在する場合、LETHE tokenが一意な64桁lowercase hexでない場合、または
親directoryが存在しない場合はfail-fastする。既存secretの上書きやtokenのfallbackは
行わない。

1. 旧Naniholdのownership assignmentをmachine-readable JSONで固定する。現在の
   cutover決定は15 sourceすべて`space:personal-primary`であり、過去sessionを
   sourceごとのConversationとして維持する。旧内部Node senderをownerへ推測変換しない。
2. Claude、Codex、Intercom、LETHE Personal、旧Nanihold、現況の全sourceをdry-runする。
3. assignmentのsource集合がlegacy scanと完全一致することを確認し、manifestとreceiptの
   件数・bytes・digest・cursorを一致させる。
   cursor付きの全page照合中はNaniholdの監査Eventをappendしない。終端までの完全走査と
   receipt集合照合を先に完了し、その後にpage参照の監査証拠をappendする。この順序を
   変えるrunbookやfallback readerは使用しない。
4. Intercom受付停止は新Naniholdのactivation/history/Conversation APIとLETHE import先が
   readyになってから行う。停止後にpending 0を再確認し、最終deltaをexportする。
5. Interface Pilotが再開を説明するためのcanonical ConversationとSurfaceBindingを作成する。
   `reorientation_conversation_id`は対象Personal DataSpaceとownerに一致する既存
   Conversationでなければならない。
6. current Work Graph snapshot、LETHE activation handoff、
   `reorientation_conversation_id`を同じ`/api/history/imports` requestへ登録し、
   `HISTORY_IMPORTED`を確認する。
7. reorientationを開始する。Interface PilotはLETHE HistoryReaderで必要箇所だけをpage照会する。
8. 全session coverage、citation、open commitment、最新現況を満たし、canonical
   Conversationを参照するAssessmentを確認する。
9. Assessmentに実在する未完WorkItemが1件以上含まれなければ承認せず、
   Web UIまたは`vsm reorientation revise --reason missing_resume_work_item`で
   `REORIENTATION_ONLY`へ戻す。provider session checkpointと使用量は保持し、
   修正済みcompact contractで明示的に再開する。
10. ownerが訂正・承認する。全resume対象、route、PilotHostを無変更でpreflightした後だけ
   `ACTIVE`となり、依存関係上開始可能な実WorkItemがdispatchされる。
   それ以前はExecutionとEffectがfail-fastする。

ターミナルではworkspace IDやassessment IDを手入力しません。

`vsm reorientation start/revise/approve`はimport receipt全文、会話本文、
raw履歴をstdoutへ出さない。stdoutは`state`、`assessment_ready`、
`reorientation_error`だけから成るcompact statusである。長文の履歴、
Assessment根拠、request payloadはcontent-addressed documentとして保存・参照し、
terminal表示用に再展開しない。

```powershell
vsm tui `
  --base-url https://nanihold.local `
  --bearer-token-env NANIHOLD_API_BEARER_TOKEN `
  --device-id device:operator

vsm reorientation start `
  --base-url https://nanihold.local `
  --bearer-token-env NANIHOLD_API_BEARER_TOKEN `
  --device-id device:operator `
  --idempotency-key reorientation:20260720

vsm reorientation approve `
  --base-url https://nanihold.local `
  --bearer-token-env NANIHOLD_API_BEARER_TOKEN `
  --device-id device:operator `
  --idempotency-key activation:20260720 `
  --correction "ownerの訂正内容"

# Assessmentが実WorkItemを選んでいない場合だけ実行する
vsm reorientation revise `
  --base-url https://nanihold.local `
  --bearer-token-env NANIHOLD_API_BEARER_TOKEN `
  --device-id device:operator `
  --idempotency-key reorientation-revision:20260720 `
  --reason missing_resume_work_item
```

`vsm reorientation approve`は現在のAssessmentとConversationをProjectionから取得し、
実在するresume WorkItemが空ならAPIを呼ぶ前に停止する。
status、TUI、keepaliveはモデルを呼ばない。Web ownerはBearer tokenを貼り付けず、
次の一回限りbootstrap linkを使う。Secure cookieのためHTTPSが必須である。

Web owner用codeは次で発行します。codeとlinkだけが表示され、保存されるのはhashです。

```powershell
vsm owner-bootstrap `
  --config vsm.toml `
  --base-url https://nanihold.local `
  --lifetime-seconds 300 `
  --idempotency-key bootstrap:20260720
```

## 5. 障害

### LETHE unavailable

状態変更と owner response を開始しません。未保存の命令を Pilot へ渡しません。backend fallback と local spool はありません。

HistoryReaderもLETHEのindexed APIだけを使用します。応答がsize上限を超えた場合は停止し、
page cursorを修正します。Nanihold側で全blob検索へfallbackしません。

### PilotHost disconnected

接続先 Execution を pause します。Node と WorkItem は残ります。再接続は device identity、最後の ack cursor、Event tail で行います。

### Effect result unknown

`UNKNOWN` として停止し、Effect idempotency key で外部状態を照合します。推測で success にしません。

### RequestedActualModelMismatch

exact selectionの応答を破棄して Execution を停止します。Router が公開済み候補から
再選択します。受信した別 model の結果を採用しません。`provider_configured`の
Interface Pilotには固有modelを要求しないため、このmodel名比較を適用せず、actual modelを
receiptとusageへ保存します。

### Local verification

`.local-verification/`はsecretと永続Lakeを含むためcommitしません。`local-review.cmd down`はデータを消さず、再起動時のcommissioningは既存Eventとcandidateを厳密照合します。ローカル検証Composeは専用project名で通常の開発Composeから隔離します。

## 6. Token Efficiency Lab

通常の status と週次判定はモデルを呼びません。一件で即時調査する事象は classifier、model substitution、full-history resend、model-call polling、false-complete です。同種 20 WorkItem 以上で承認 baseline から 10% 以上悪化した場合も調査します。

モデル評価が不可避なら、検証用の安価なexact candidate allowlistに登録された独立 Judgeと`low` effortだけを許可します。暫定的なモデル名を禁止語として判定しません。AI Judge の confusion matrix を deterministic/human truth と同時に更新します。

## 7. 完了

次の一つでも偽なら WorkItem は completed になりません。

- acceptance satisfied
- required tests passed
- blocking deviations が空
- independent S3* gate passed
- integration branch merged
- remote push succeeded

deploy は completion 条件に含めません。

## 8. HA配備ゲート

`deploy/ha`は設計と静的契約であり、現状のimageを配備可能と宣言しません。
runtime contract receiptがNanihold production config、PilotHost transport、
LETHE canary/restore/projection、canonical backupを実装済みtest digest付きで
証明しない限り`RUNTIME_CONTRACT_UNAVAILABLE`で停止します。NAS、管理者Hyper-V、
ISO、SSH、secret、実PostgreSQL、RPO/RTO測定も必須です。fallbackや単一nodeへの
縮退運転は行いません。
