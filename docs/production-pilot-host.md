# Production PilotHost

`scripts/production_pilot_host.py` は Nanihold Kernel の外で Claude Code と
Codex CLI を起動する device 境界です。Kernel は provider 固有の CLI flag、
permission classifier、session、MCP 設定を保持しません。PilotHost は fallback
を行わず、要求 candidate と provider が報告した actual model が一致しない結果を
失敗 receipt として保存します。

このスクリプトは production 用です。`scripts/local_pilot_host.py` は隔離された
ローカル確認専用で、契約・candidate・endpoint を共有しません。

## 起動条件

設定 JSON の required top-level field は次の9個です。未定義 field、欠落 field、
空文字、存在しない working directory、MCP allowlist と server 定義の差は起動時
エラーです。CLI の厳密な版一致は起動条件ではなく、preflight 有効時の dispatch
直前検証と実挙動検証で扱います。

- `pilot_host_id`
- `device_id`
- `device_certificate_sha256`
- `bearer_token_env`
- `bind_host`
- `bind_port`
- `receipt_store_path`
- `claude`
- `codex`

`preflight` は任意の top-level field です。省略時は read-only または非 Windows の
既存経路を維持します。ただし Windows で `workspace-write` を使う場合は、通常 sandbox
で実行するために契約適合ゲートを明示しなければなりません。ゲートを有効にする場合は、
次の全フィールドを明示します。

```json
{
  "preflight": {
    "enabled": true,
    "kernel_config_path": "/etc/nanihold/vsm.toml",
    "cli_version_files": {
      "codex-cli": "/usr/local/lib/node_modules/@openai/codex/package.json",
      "claude-code": "/usr/local/lib/node_modules/@anthropic-ai/claude-code/package.json"
    },
    "cache_path": "/var/lib/nanihold-pilot/preflight-cache.json",
    "instance_fingerprint": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "environment_contract": {
      "supported_shells": ["posix"],
      "workspace_writable": true,
      "minimum_memory_mb": 4096,
      "required_sandbox": "workspace-write",
      "supported_sandboxes": ["workspace-write"],
      "path_mapping_names": ["workspace-root"],
      "adapters": {
        "codex-cli": {
          "required_endpoints": ["api.openai.com"],
          "minimum_cli_version": "0.144.5"
        },
        "claude-code": {
          "required_endpoints": ["api.anthropic.com"],
          "minimum_cli_version": "2.1.215"
        }
      }
    }
  }
}
```

`preflight.kernel_config_path` が設定されている場合、Kernel の `vsm.toml` が
`preflight_enabled`、版ファイル、cache、契約、ローカル契約アーティファクト、
EnvironmentInstance binding の正本です。PilotHost JSON の同名値は Kernel 設定が
取得できない構成でだけ使う明示的な fallback です。Kernel と JSON の値が両方ある場合、
JSON 側で Kernel の値を上書きすることはできません。

`cli_version_files` は adapter 名から `package.json` の `version` を読むファイルへの
写像です。JSON 以外ならファイル全体を版文字列として読みます。PilotHost は対象
adapter のファイルを毎回 `stat` と読み取り
だけで確認し、`codex --version` を版検知のために起動しません。読めない版ファイル、
契約不適合、壊れたキャッシュはすべて fail-fast します。キャッシュは一時ファイルの
fsync 後に rename して永続化します。

`environment_contract` は `vsm.environment.EnvironmentContract` の正規スキーマです。
共通能力要件に加えて、`adapters` に adapter ごとの endpoint と最低CLI版を宣言します。
fingerprint を設定JSONへ重複記載せず、PilotHostが
`environment_fingerprint(contract)` で計算し、各 adapter candidate の宣言値と厳密に
照合します。preflight runner は sandbox に加えて `workspace_writable`、
`endpoint_reachable`、`memory_bytes`、`shell`、`path_mappings` を実測証拠として返し、
契約の全必須能力が確認できなければ本実行へ進みません。

dispatch 直前の検証タプルは `(adapter, cli_version, required_sandbox,
environment_fingerprint)` です。版ファイルの mtime もキャッシュ整合性に含め、
タプルと mtime が一致した dispatch では対象 adapter の preflight 試走を省略します。不一致時は
対象CLIを1回試走し、rollout の `sandbox_policy` を読み、契約に合格した場合だけ実行へ
進みます。

`vsm.preflight.PreflightGate` の接続面は次のとおりです。

- `preflight_runners[adapter](verification_tuple) -> PreflightObservation | Mapping` は試走と
  rollout 読み出しを注入するフックです。
- `evidence_hook(PreflightEvidence)` は合格証拠を Operational Ledger へ渡すための
  記録フックです。`EnvironmentInstanceService.preflight_evidence_hook(instance_id,
  idempotency_key_prefix=...)` を渡すと、契約・instance fingerprintを照合して
  `environment_instance_verified` Eventへ証拠を保存します。
- `declaration_event_hook(DeclarationUpdateEvent)` は FAV-06 の決定論的な
  `candidate.adapter_version` 更新イベントを渡す監査フックです。

preflight の合格後、デフォルト updater は candidate 宣言の
`adapter_version` が変わった場合に実測版へ機械的に更新し、`field`、`occurred_at`、
`from_value`、`to_value`、`source`、`reason` を含むイベントを発行します。失敗時には
宣言更新・キャッシュ保存・本実行のいずれも行いません。

Kernel 側の次の設定を PilotHost が読み取れる場合、PilotHost はローカル版付き契約を
`LocalEnvironmentContractStore` から取得し、`EnvironmentInstance.from_contract(...)`
で実体を構築します。artifact の契約 fingerprint、候補の fingerprint、instance
fingerprint、Ledger DataSpace はすべて厳密に一致しなければなりません。

```json
{
  "preflight": {
    "environment_contract_artifact": {
      "store_path": "/var/lib/nanihold-pilot/environment-artifacts",
      "artifact_key": "codex-workspace",
      "artifact_version": 1
    },
    "data_space_id": "space:personal-primary",
    "environment_instance": {
      "instance_id": "environment-instance:primary",
      "logical_path_bindings": {"workspace-root": "/workspace"},
      "cli_executable_path": "/usr/local/bin/codex",
      "codex_home": "/var/lib/nanihold-pilot/codex-home",
      "environment_variables": {},
      "machine_identity": {"host": "commissioned-host"}
    },
    "operational_ledger": {
      "base_url": "https://lethe.example.invalid",
      "bearer_token_env": "LETHE_BEARER_TOKEN",
      "data_space_id": "space:personal-primary",
      "timeout_seconds": 30,
      "max_page_size": 100
    }
  }
}
```

binding が設定されると、Codex の executable、workspace、`CODEX_HOME`、子 process
環境を binding から構築し、組み込み Codex 試走へ endpoint・memory・shell・logical
path・workspace write の実測を追加します。Ledger が設定されている場合は
`EnvironmentInstanceService.preflight_evidence_hook(instance_id,
idempotency_key_prefix="environment:preflight:verify")` を合格証拠へ接続します。
これらの実体設定を渡せない構成は、外部から evidence hook と runner を注入する試験用に
限られ、本番起動では fail-fast します。

## Windows 暫定 bypass と通常 sandbox

`codex.win32_codex_sandbox_bypass_enabled` は Windows native の暫定経路を制御します。
既定値は `false` です。`false` の Windows `workspace-write` は通常の
`--sandbox workspace-write` を使い、preflight が契約適合を証明していない限り起動を
拒否します。`true` を明示した場合だけ
`--dangerously-bypass-approvals-and-sandbox` を使う暫定経路が有効になります。
このフラグは WSL 切替の代替ではなく、配備時に明示的な一時措置として監査してください。
WSL 実体の構築・切替とフラグ撤去は `docs/eep-phase1-deployment.md` の手動手順です。

秘密値は設定 JSON に書きません。Nanihold RPC Bearer、LETHE history MCP Bearer、
Gateway MCP Bearer はすべて環境変数名だけを設定し、値が欠けていれば起動しません。

Windows device hostでは、secretをargvへ展開せず専用child environmentだけへ渡す。

```powershell
.\scripts\start_activation_pilot_host.ps1 `
  -PythonExecutable D:\Nanihold\.venv\Scripts\python.exe `
  -RepositoryRoot D:\Nanihold `
  -ConfigFile D:\secure\interface-activation\pilot-host.json `
  -RuntimeEnvFile D:\secure\interface-activation\runtime.env `
  -LogFile D:\secure\interface-activation\pilot-host.log `
  -PidFile D:\secure\interface-activation\pilot-host.pid `
  -ReadyTimeoutSeconds 30
```

PID receiptが既に存在する場合、secret集合が完全一致しない場合、または起動直後に
processが終了した場合はfail-fastする。Windowsの`Start-Process -Environment`は
`PATH`を特殊再構成するため使用しない。ランチャーprocessへ起動の瞬間だけ3個の
activation secretを設定し、子processへ親環境をそのまま継承させた直後に元の値へ
復元する。従ってCLI探索に必要な親の`PATH`を変更しない。可視windowやsecretを含む
command lineは作らない。

起動成功は固定時間のprocess生存ではなく、設定済みdevice identityとBearerを使った
loopback `/health`が`ready`を返した時点で判定する。PID receiptはその後に
`CreateNew`で原子的に作り、既存receiptを上書きしない。ready前の終了・timeout・
identity不一致では起動したprocessを停止し、PID receiptを残さない。providerの
stdout/stderr本文はterminalへ転送せず専用fileへ保持し、起動失敗のterminal表示は
exit code、byte数、SHA-256だけに制限する。launcher自身の成功出力もready状態と
PID receipt作成の1行だけである。
このlauncherはWindows Service、Task Scheduler、container supervisorそのものではない。
親process treeを終了時に回収する実行基盤では、継続運用用supervisorの配下でlauncherまたは
foreground serverを保持する。通常terminalのbackground childが永続すると推測せず、
PIDと認証付きhealthの両方で生存を確認する。

Claude candidate は次の値に固定します。

```text
adapter        = claude-code
provider       = anthropic
selection      = provider_configured
effort         = high
```

`claude.max_request_document_bytes`は必須で、production推奨値は`32768` bytesである。
content-addressed request documentがこの値を超える場合、PilotHostはdocumentを書込まず、
Claude CLIを起動せずにfail-fastする。自動切詰め、要約model、互換・fallback経路は使わない。
この境界は48,337-byteの長大入力を拒否し、`history_max_result_bytes=24000`で
page化したcompact current-state indexとassessment contractを含む通常の初回
reorientation documentを許容するための運用上限である。

owner向けCLIの`vsm reorientation start/revise/approve`も同じ出力原則に従う。import receipt全文や
request document本文はstdoutへ出さず、`state`、`assessment_ready`、
`reorientation_error`だけをcompactに返す。長文はcontent-addressed documentの
digest/refで追跡する。
productionの初回再オリエンテーションは`reorientation_max_tool_rounds=8`で上限を固定する。
各turnは厳密に履歴読取1件またはAssessment提出1件だけであり、上限到達時は追加model callを
行わずfail-fastする。

`provider_configured` candidateには`model_snapshot` fieldを含めません。
`fallback_model` は設定契約に存在しません。Claude Code には
`--effort high`だけを渡し、`modelUsage`のactual modelを証拠として記録します。
Interface Pilotは固有modelを要求しないためmodel名比較を行いません。codingの
exact selectionは`RequestedActualModelMismatch`で停止します。`root_session_id`を指定したturnは
`fork_session=true`が必須で、`--resume <root> --fork-session`として起動します。
root transcriptへ直接turnを追加しません。

permission mode は設定とrequestの双方で明示し、完全一致させます。

| Pilot mode | Claude Code flag | classifier | 条件 |
|---|---|---:|---|
| `sandboxed_bypass` | `--permission-mode bypassPermissions` | 0 | SandboxProfile certificate SHA-256必須 |
| `managed_permissions` | `--permission-mode auto` | 計測対象 | SandboxProfile certificate禁止 |
| `observe_only` | `--permission-mode plan` | 0 | SandboxProfile certificate禁止 |

Interface Pilotには組み込みtoolを公開しません。`--tools ""`と
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

Interface Pilotの通常turnです。bodyは以下のfieldだけです。

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

履歴索引を読むためのInterface Pilot turnです。通常turnの共通fieldに加えて次だけを
受けます。

```text
objective, session_index_ref, open_commitment_refs, current_state_ref
```

返せるactionは`history.read`と`reorientation.submit`だけです。WorkItem作成、
Effect、完了提案などを含むoutputは
`ReorientationEffectForbidden`で失敗します。
`reorientation.submit`は`understanding`、全`active_missions`、全
`decisions_and_constraints`に対応するcitationを必須とします。根拠が足りない場合は
部分的Assessmentを提出せず、履歴読取を1件だけ要求します。表示文とAssessment本文には
schema上の文字数・件数上限を設け、長大な完了説明を標準出力へ流しません。

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
Codexはinput/cache/output/reasoning_output/totalを保存します。

## budgetとtimeout

Claudeはrequest値がhost上限以下か確認し、CLIの`--max-budget-usd`と
subprocess timeoutの両方で制限します。

Codexはrequest token budgetがhost上限以下かを開始前に確認し、終了時のusageも
同じ上限で検査します。現行`codex exec`には文書化されたhard token ceiling flagが
ないため、実行中の強制上限はtimeout、token上限は結果検査です。この差を隠す
fallbackはありません。

## Codex actual-model gate

PilotHostはcodex自身が書くsession rollout(`CODEX_HOME/sessions/.../rollout-*-<thread_id>.jsonl`)の
`turn_context` recordから、実際に解決されたmodelとreasoning effortを読み取ります。

```json
{
  "type": "turn_context",
  "payload": {
    "model": "gpt-5.6-luna",
    "effort": "xhigh"
  }
}
```

`thread_id`は`codex exec --json`のstdout stream(`thread.started` event)から取得します。
work execution receiptの`provider_session_id`には、このeventから取得したthread IDを
そのまま保存します。thread IDを取得した後のactual model、usage、structured resultの
検証失敗でも、取得済みのIDは失敗receiptへ残します。
`turn_context`をuniqueに特定できない、読めない、model/effortが無い、いずれの場合も
`ActualModelUnverifiable`でfail closedします。
thread eventが無い、またはthread IDが空の場合も`ProviderProtocolError`で失敗し、
`provider_session_id: null`のまま成功扱いにはしません。

実測背景(codex-cli 0.144.5): `codex exec --json`のstdout event stream
(`thread.started` / `turn.started` / `item.completed` / `turn.completed`)は
actual model/effortを一切含まず、`turn.completed`は`usage`のみを返します。公開CLIにも
これらをstreamへ出させるflagはありません。そのためactual modelの確認元は、codexが同一runで
authoritativeに書く`turn_context`recordです。これはrequested flagからの推定ではなく、
codex自身の解決結果の直接読み取りであり、fail-fast原則を維持します。
`usage`は`input_tokens` / `cached_input_tokens` / `output_tokens` / `reasoning_output_tokens`を
正確なcontractとして保存します(0.144.5で`reasoning_output_tokens`が追加)。
CODEX_HOMEはcodex subprocessと同一の解決(`CODEX_HOME` env、なければ`~/.codex`)を使います。

## backend接続の実装状態

production runtimeは`vsm.pilot.production_host.ProductionPilotHostClient`を使用し、
local verification専用の`vsm.interface.pilot_host.PilotHostInterfacePilot`は使用しません。
次のbackend接続は実装済みですが、productionのPilotHost、LETHE、承認済みRouteSnapshotを
接続して稼働確認したという意味ではありません。

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

Naniholdのproduction configは起動時にPilotHost health、active RouteSnapshot、LETHE
Operational Ledgerを厳密照合します。いずれかが未commissionまたは不一致なら起動は失敗し、
local verificationや旧backendへfallbackしません。

`/v1/reorientation-turn`にはprojection referenceだけでなく、直前の監査済み
`history_result`（本文を含む`result_json`、result Event ID、cursor、source cursor）と、
`assessment_contract`を渡します。初回contractはimport ID、canonical Conversation、
verified session index refとsession count、materialize済みopen commitment、既知WorkItem、
最小history cursorを含む。全session IDをproviderへ列挙しない。Pilotはこれらを
推測・置換できず、Assessmentのcitationは渡されたhistory result Event IDだけを使う。
open commitmentはInterface Pilot turn前に全pageを決定論的に取得してcanonical Conversationへ
materializeし、再起動後のresume packにも残す。

初回root forkだけが完全なAssessment contractを受け取る。以後のhistory roundは
`import_id`、canonical Conversation、contract SHA-256、session index ref/count、
open commitment ID、実在WorkItemのID・title・description・acceptance・state、
最小history cursorからなるcompact referenceを受け取り、
`assessment_contract_included: false`でなければ拒否する。Assessmentはsession ID集合をechoせず、
`covered_session_index_ref`と`covered_session_count`を返す。Nanihold gateはverified
receiptとの完全一致、および`list_sessions`が先頭から最終pageまで到達したことを
決定論的に検査する。

session indexはInterface Pilotのtool loopでは取得しない。Naniholdがreorientation開始時に
`list_sessions`の全pageをmodel-freeで走査し、receiptとのID集合・件数一致を監査する。
Interface Pilotへはindex ref/count、source kind別count、最初・最後のmessage時刻、全page監査Event ID
だけを渡す。従ってsession数が増えてもmodel-call pagination、full list再送、pollingは発生しない。

長いInterface/Reorientation情報はcontent-addressed request document（canonical JSON）として
原子的に保存する。documentは`receipt_id`とrequest SHA-256を含み、ファイル名と短いstdin指示が
document SHA-256を保持するためreceiptと相互に照合できる。Claude CLIには
`--append-system-prompt-file`でdocumentを読ませる。stdinは
digest付きの短文だけ（256 bytes以下）とし、履歴本文、history result、assessment contractを
argvやstdinへ直接載せない。stdout/stderrはcaptureし、Windowsでは`CREATE_NO_WINDOW`で起動する。
captureしたprovider I/Oも別のcontent-addressed JSONへ保存し、receipt ID、request SHA-256、
return code、各streamのbytes・digest・raw captureを保持する。receiptの安全なerrorには
この文書digestだけを記録し、raw streamをterminalやAPI responseへ流さない。
`shell=False`を維持し、この契約を満たさないCLI版は起動前検証で停止する。

## テスト

実providerを起動しない固定subprocess mockだけを使います。

```powershell
wsl --cd /mnt/d/userdata/docs/projects/Nanihold_OS -- `
  docker compose run --rm --no-deps app `
  python -m pytest tests/test_production_pilot_host.py -q
```

実providerへのmodel callはありません。テストは固定subprocess mockとcheap exact candidate allowlist／`low`の構成gateで、Claude/Codex argv、root fork、
strict MCP、secret非露出、structured schema、任意shell field拒否、
model mismatch、sandboxed modeのclassifier 0、idempotency、transport unknownを
検査します。暫定的なモデル名を禁止語として使いません。
