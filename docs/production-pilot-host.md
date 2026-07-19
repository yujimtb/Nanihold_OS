# Production PilotHost

`scripts/production_pilot_host.py` は Nanihold Kernel の外で Claude Code と
Codex CLI を起動する device 境界です。Kernel は provider 固有の CLI flag、
permission classifier、session、MCP 設定を保持しません。PilotHost は fallback
を行わず、要求 candidate と provider が報告した actual model が一致しない結果を
失敗 receipt として保存します。

このスクリプトは production 用です。`scripts/local_pilot_host.py` は隔離された
ローカル確認専用で、契約・candidate・endpoint を共有しません。

## 起動条件

設定 JSON の top-level field は次の9個だけです。未定義 field、欠落 field、
空文字、存在しない working directory、MCP allowlist と server 定義の差、
CLI version と candidate adapter version の差は起動時エラーです。

- `pilot_host_id`
- `device_id`
- `device_certificate_sha256`
- `bearer_token_env`
- `bind_host`
- `bind_port`
- `receipt_store_path`
- `claude`
- `codex`

秘密値は設定 JSON に書きません。Nanihold RPC Bearer、LETHE history MCP Bearer、
Gateway MCP Bearer はすべて環境変数名だけを設定し、値が欠けていれば起動しません。

Claude candidate は次の値に固定します。

```text
adapter        = claude-code
provider       = anthropic
model_snapshot = claude-fable-5
effort         = high
```

`fallback_model` は設定契約に存在しません。Claude Code には
`--model claude-fable-5 --effort high`を渡し、`modelUsage`が別modelを示すと
`RequestedActualModelMismatch`で停止します。`root_session_id`を指定したturnは
`fork_session=true`が必須で、`--resume <root> --fork-session`として起動します。
root transcriptへ直接turnを追加しません。

permission mode は設定とrequestの双方で明示し、完全一致させます。

| Pilot mode | Claude Code flag | classifier | 条件 |
|---|---|---:|---|
| `sandboxed_bypass` | `--permission-mode bypassPermissions` | 0 | SandboxProfile certificate SHA-256必須 |
| `managed_permissions` | `--permission-mode auto` | 計測対象 | SandboxProfile certificate禁止 |
| `observe_only` | `--permission-mode plan` | 0 | SandboxProfile certificate禁止 |

Fableには組み込みtoolを公開しません。`--tools ""`と
`--strict-mcp-config`を常に指定します。`--safe-mode`はMCP自体を無効化するため
指定しません。MCP server名は設定の`allowlist`と
`servers`が完全一致し、candidate toolsetは
`mcp__<allowlisted-server>__<typed-tool>`だけを許可します。Bearer値はargvや
logへ出さず、MCP clientが指定環境変数から取得します。

Claude JSON resultの`permission_denials`は必須usageです。
`managed_permissions`ではdenial件数からclassifier作動を計測します。
`sandboxed_bypass`で1件でも返れば設定不整合とみなし
`ClassifierUnexpected`で結果を採用しません。

Codex candidateは`codex-cli/openai`でなければ起動しません。各Executionは
以下を必ずCLIへ明示します。

```text
codex exec --json --output-schema <generated-schema>
  --model <exact snapshot>
  -c model_reasoning_effort="<exact effort>"
  --cd <allowlisted exact cwd>
  --sandbox <read-only|workspace-write>
  --strict-config --ignore-user-config
```

MCPはroot `mcp_servers` table全体を単一`-c`で置換し、ambient設定とのmergeを
防ぎます。PilotHostはshellを使わず、すべてargv配列で起動します。

## RPC

全endpointは次の4 headerが完全一致しなければ`401`です。

```text
Authorization: Bearer <secret>
X-Nanihold-Pilot-Host-Id: <pilot_host_id>
X-Nanihold-Device-Id: <device_id>
X-Nanihold-Device-Certificate-Sha256: <64 lowercase hex>
```

request bodyの`device_identity`も同じidentityでなければなりません。

### `POST /v1/interface-turn`

Fableの通常turnです。bodyは以下のfieldだけです。

```text
receipt_id, idempotency_key, device_identity, candidate, permission_mode,
max_budget_usd, timeout_seconds, root_session_id, fork_session, event_delta,
resume_pack, owner_text
```

新しいrootを作る場合だけ`root_session_id=null`、`fork_session=false`、
最小`resume_pack`を許します。rootを再開する通常turnは`resume_pack=null`とし、
event deltaだけを渡します。全履歴やraw transcriptを受けるfieldはありません。

Claudeの1回のstructured outputは`display_text`と、現在の
`vsm.interface.models.InterfaceAction` discriminated unionで検査します。別の
要約modelは呼びません。

### `POST /v1/reorientation-turn`

履歴索引を読むためのFable turnです。通常turnの共通fieldに加えて次だけを
受けます。

```text
objective, session_index_ref, open_commitment_refs, current_state_ref
```

返せるactionは`history.read`と`reorientation.submit`だけです。WorkItem作成、
Effect、完了提案などを含むoutputは
`ReorientationEffectForbidden`で失敗します。

### `POST /v1/work-executions`

Codex coding S1へ渡すhandoffは以下だけです。

```text
receipt_id, idempotency_key, device_identity, candidate, execution_id,
work_item, unmet_acceptance, event_delta, artifact_refs, cwd, sandbox,
token_budget, timeout_seconds
```

`command`、`argv`、`shell`、汎用`context`は契約に存在せず、追加するとproviderを
起動する前に`422`です。`work_item.objective`はprompt内のJSON値であり、
PilotHostがコマンドとして評価することはありません。

Codex resultは`summary`、全未達acceptanceの同順評価、artifact参照、
event note、`completed`を持つschemaで検査します。未達acceptanceを欠落した結果、
未達があるのに`completed=true`とした結果は採用しません。

### receiptとreconciliation

POSTはprovider起動前にSQLiteへ`in_progress` receiptを原子的に保存します。
成功と失敗の双方で、actual model、provider session、providerが報告したusage、
typed resultまたは安全なerror codeを保存します。同じidempotency keyと同じ
request digestはproviderを再実行せず同じreceiptを返します。別requestへのkey再利用
は`409`です。

```text
GET /v1/receipts/{receipt_id}
```

POST応答が到達したか不明な場合はGETで照合します。ホストがprovider実行中に
再起動した場合、残存`in_progress`を`transport_unknown`へ変換します。成功とは
推定せず、Nanihold側のartifact/Effect reconciliationが必要です。

`usage`はproviderがusageを返さなかった失敗では`null`です。0を捏造しません。
Claudeはinput/cache creation/cache read/output/cost/duration/classifier modeを、
Codexはinput/cache/output/totalを保存します。

## budgetとtimeout

Claudeはrequest値がhost上限以下か確認し、CLIの`--max-budget-usd`と
subprocess timeoutの両方で制限します。

Codexはrequest token budgetがhost上限以下かを開始前に確認し、終了時のusageも
同じ上限で検査します。現行`codex exec`には文書化されたhard token ceiling flagが
ないため、実行中の強制上限はtimeout、token上限は結果検査です。この差を隠す
fallbackはありません。

## Codex actual-model gate

PilotHostは`codex exec --json`の`turn.completed`に次が無ければ
`ActualModelUnverifiable`で失敗させます。

```json
{
  "model": "gpt-5.6-sol",
  "model_reasoning_effort": "xhigh"
}
```

2026-07-20時点の公開CLI referenceは`--model`、`-c`、`--json`、
`--output-schema`を説明していますが、JSONL eventがactual model/effortを返す契約は
明記していません。導入するCodex CLI版でこの2 fieldを実測確認できるまでcoding S1を
commissionしてはいけません。fieldを返さない版に対し、requested flagからactualを
推定する互換経路は追加しません。

## backend接続に必要な変更

現在の`vsm.interface.pilot_host.PilotHostInterfacePilot`はlocal verification契約
（単一candidate health、`candidate/owner_text/context` request、直接response）を
使用しています。production cutoverにはbackend側で次が必要です。

1. healthを`candidates.interface`と`candidates.coding_s1`のexact keyで照合する。
2. Bearerに加え3つのdevice headerを全RPCへ送る。
3. owner message blob保存後にevent deltaとreference packを構成し、新しい
   `/v1/interface-turn` receipt契約を使う。
4. activation controllerから`/v1/reorientation-turn`を呼び、owner承認前は他の
   endpointを呼ばない。
5. dispatcherから`/v1/work-executions`を呼び、WorkItem、未達acceptance、
   event delta、digest付きartifact参照だけを渡す。
6. POSTの接続結果が不明なら同じrequestを再送せず
   `GET /v1/receipts/{id}`で照合する。
7. failed/transport_unknown receiptをExecution failure/paused/reconciliation
   Eventへ変換し、actual-model mismatchを成果として採用しない。

これらが接続されるまではproduction PilotHostを起動しても、既存backendから
自動的に利用されません。

## テスト

実providerを起動しない固定subprocess mockだけを使います。

```powershell
wsl --cd /mnt/d/userdata/docs/projects/Nanihold_OS -- `
  docker compose run --rm --no-deps app `
  python -m pytest tests/test_production_pilot_host.py -q
```

Fable/Opusへのmodel callはありません。テストはClaude/Codex argv、root fork、
strict MCP、secret非露出、structured schema、任意shell field拒否、
model mismatch、sandboxed modeのclassifier 0、idempotency、transport unknownを
検査します。
