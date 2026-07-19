# Operations

## 1. 必須設定

`vsm.toml` は repository へ commit せず、`config/nanihold.example.toml` を基に環境ごとに作成します。次はすべて必須です。

- LETHE base URL、Bearer token 環境変数、DataSpace ID、Lake location
- AuditPolicy、ControlPolicy
- Pilot mode、PilotHost device identity
- `sandboxed_bypass` の SandboxProfile certificate と digest
- Interface Pilot の exact candidate。現在の既定は `claude-fable-5 / high`
- candidate ごとの versioned benchmark prior
- active RouteSnapshot ID
- API Bearer token、CORS origin

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

Interface Adapterは認証済みPilotHostへRPCし、起動時にexact candidate keyを照合します。応答時はClaude Codeの`modelUsage`が示すactual modelをrequested snapshotと照合します。top-level表示名やaliasから実モデルを推定しません。

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
  --bearer-token-env NANIHOLD_API_BEARER_TOKEN
vsm inspect events \
  --base-url http://localhost:8000 \
  --bearer-token-env NANIHOLD_API_BEARER_TOKEN \
  --after-cursor 0 \
  --limit 250
```

resource は `data-spaces`、`nodes`、`work-items`、`executions`、`events`、`conversations`、`pilot-hosts`、`model-registry`、`route-snapshots`、`token-lab` です。

production objective は `quality_max` です。production exploration は禁止です。証拠更新後は古い snapshot が stale になり、再起動時に失敗します。

## 4. 障害

### LETHE unavailable

状態変更と owner response を開始しません。未保存の命令を Pilot へ渡しません。backend fallback と local spool はありません。

### PilotHost disconnected

接続先 Execution を pause します。Node と WorkItem は残ります。再接続は device identity、最後の ack cursor、Event tail で行います。

### Effect result unknown

`UNKNOWN` として停止し、Effect idempotency key で外部状態を照合します。推測で success にしません。

### RequestedActualModelMismatch

応答を破棄して Execution を停止します。Router が公開済み候補から再選択します。受信した別 model の結果を採用しません。

### Local verification

`.local-verification/`はsecretと永続Lakeを含むためcommitしません。`local-review.cmd down`はデータを消さず、再起動時のcommissioningは既存Eventとcandidateを厳密照合します。ローカル検証Composeは専用project名で通常の開発Composeから隔離します。

## 5. Token Efficiency Lab

通常の status と週次判定はモデルを呼びません。一件で即時調査する事象は classifier、model substitution、full-history resend、model-call polling、false-complete です。同種 20 WorkItem 以上で承認 baseline から 10% 以上悪化した場合も調査します。

モデル評価が不可避なら `low` の安価な独立 Judge だけを許可します。Fable と Opus は拒否します。AI Judge の confusion matrix を deterministic/human truth と同時に更新します。

## 6. 完了

次の一つでも偽なら WorkItem は completed になりません。

- acceptance satisfied
- required tests passed
- blocking deviations が空
- independent S3* gate passed
- integration branch merged
- remote push succeeded

deploy は completion 条件に含めません。
