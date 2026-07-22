# EEP Phase 1 配備手順

この手順は、EnvironmentContract / EnvironmentInstance / preflight を現行の
Windows native 本番 PilotHost へ反映し、fingerprint 変更に伴う RouteSnapshot を
再発行するための手動手順です。本 worktree から本番設定を直接変更したり、本番
process/APIへ接続したりしません。`<...>` はcommission済みの実値へ置き換えます。

> **重要警告: コードと設定は同一メンテナンス窓で反映する。**
>
> 本番 `pilot-host.json` の現状の `codex` には `preflight` も
> `win32_codex_sandbox_bypass_enabled` もありません。コードだけを先に配備すると、
> Windows の `workspace-write` 実行は fail-closed ゲート
> (`scripts/production_pilot_host.py:2271-2282`) により全件 `ContractError` になります。
> コード、`vsm.toml`、`pilot-host.json`、contract artifact、RouteSnapshot の切替を
> 同じメンテナンス窓で行い、設定反映前に新コードで本番 Codex を実行しません。

## 1. 反映前の準備

### 1.1 現行ファイルと前提を読み取り確認する

**実行するコマンド / 編集内容**

本番 Windows host 上で、編集前に次を実行します。

```powershell
$ProductionRoot = "D:/userdata/docs/projects/_cutover_20260720_fable_activation/production"
$VsmConfig = Join-Path $ProductionRoot "vsm.toml"
$PilotHostConfig = Join-Path $ProductionRoot "pilot-host.json"

if (!(Test-Path -LiteralPath $VsmConfig -PathType Leaf)) { throw "vsm.toml がありません" }
if (!(Test-Path -LiteralPath $PilotHostConfig -PathType Leaf)) { throw "pilot-host.json がありません" }
Select-String -LiteralPath $VsmConfig -Pattern '^\[environment_contract\]$|^environment_fingerprint|^sandbox_fingerprint|^work_cwd|^active_route_snapshot_id'
Select-String -LiteralPath $PilotHostConfig -Pattern '"preflight"|"win32_codex_sandbox_bypass_enabled"|"sandbox_fingerprint"|"working_directory_allowlist"'
```

**期待される出力 / 状態**

- 現行 `vsm.toml` には `[environment_contract]` が無い。
- `interface_pilot` と各 candidate の既存宣言値は `sha256:cca77bb58e...` である。
- `production_pilot_host.work_cwd` は `D:/userdata/docs/projects`、coding sandbox は
  `workspace-write`、active route は
  `route:coding-production-20260720-luna-first` である。
- 現行 `pilot-host.json` の `codex` に上記2キーが無い。

**失敗時の判断**

期待値と異なる場合は、別の配備・設定変更が混在している。値を推測して続行せず、
owner/S5 と現行のcommission receiptを照合してから中止または手順の入力値を更新します。

### 1.2 Windows native の実体を測定する

**実行するコマンド / 編集内容**

現行 `pilot-host.json` の値をそのまま入力に使い、実体の存在・版・書込能力・メモリ・
endpointを測定します。`CODEX_HOME` は実行時に使用する実値を記録します。

```powershell
$CodexExecutable = "C:/Users/mitob/AppData/Roaming/npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
$Workspace = "D:/userdata/docs/projects"
$CodexHome = "C:/Users/mitob/.codex"

foreach ($Path in @($CodexExecutable, $Workspace, $CodexHome)) {
    if (!(Test-Path -LiteralPath $Path)) { throw "実体パスがありません: $Path" }
}
& $CodexExecutable --version
$MemoryMb = [math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB)
if ($MemoryMb -lt 4096) { throw "minimum_memory_mb=4096 を満たしません: $MemoryMb MB" }
$Probe = Join-Path $Workspace ".eep-preflight-write-probe-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType File -LiteralPath $Probe -ErrorAction Stop | Out-Null
Remove-Item -LiteralPath $Probe -Force -ErrorAction Stop
Test-NetConnection api.openai.com -Port 443 -InformationLevel Quiet
```

**期待される出力 / 状態**

`0.144.5`、workspaceの作成・削除成功、`api.openai.com:443` の `True`、4 GiB以上の
物理メモリが確認できる。Windows native の実装は preflight runner により
`shell = "powershell"` と測定される。

**失敗時の判断**

どれか一つでも失敗したら、契約値を弱めずに停止します。endpoint、メモリ、CLI版、
workspace、`CODEX_HOME` の実値を再commissionし、owner/S5が契約変更を承認するまで
次へ進みません。

### 1.3 契約・実体 fingerprint を計算する

**実行するコマンド / 編集内容**

以下は `vsm/environment/contracts.py` と `vsm/environment_instance.py` の正規化・
ハッシュ式を直接呼び出す一行です。値を変更した場合は必ず同じコマンドを再実行します。
本番の実体で実行できるPython環境が無い場合は、同じコードをこのリポジトリのDocker
`app`サービス内で実行します。

```powershell
docker compose -p eep-runbook run --no-deps --rm app python3 -c "from vsm.environment import EnvironmentContract, environment_fingerprint; from vsm.environment_instance import compute_instance_fingerprint; c=EnvironmentContract.model_validate({'supported_shells':['powershell','posix'],'required_endpoints':['api.openai.com'],'workspace_writable':True,'minimum_memory_mb':4096,'supported_sandboxes':['workspace-write'],'required_sandbox':'workspace-write','path_mapping_names':['workspace-root'],'minimum_cli_version':'0.144.5'}); print('environment_contract='+environment_fingerprint(c)); print('instance='+compute_instance_fingerprint(logical_path_bindings={'workspace-root':'D:/userdata/docs/projects'},cli_executable_path='C:/Users/mitob/AppData/Roaming/npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe',codex_home='C:/Users/mitob/.codex',environment_variables={},machine_identity={'execution':'windows-native','os':'windows'}))"
```

**期待される出力 / 状態**

この入力値(`supported_shells = ["powershell", "posix"]`)の計算コマンド実行結果は次のとおりです。

```text
environment_contract=environment-contract-sha256:9b16d4c3c11cd670696d9edb9e3aa1ff8c56450c1d08aa05099ab356436eea2b
instance=a349962aeb65f08fb8b5e5b7cebffbad573d1c595f2158cf9d2fc84de536b510
```

新しい `environment_fingerprint` はこの `environment-contract-sha256:...` 全体を
使います。既存の手書き値 `sha256:cca77bb58e...` は再利用しません。

**失敗時の判断**

出力が異なる場合は、入力値または実体が既知例と違う。既知値を手入力で合わせず、
測定値を再確認して出力値をcommission receipt、artifact、全candidateへ一貫して使います。

## 2. 設定編集前のバックアップ（必須）

### 2.1 `vsm.toml` と `pilot-host.json` を日時付きで保存する

**実行するコマンド / 編集内容**

2〜4章の編集より前に、本番 host 上で2ファイルを同じ時刻IDで保存します。

```powershell
$Stamp = Get-Date -Format "yyyyMMdd-HHmmssfff"
$VsmBackup = "$VsmConfig.$Stamp.bak"
$PilotHostBackup = "$PilotHostConfig.$Stamp.bak"
if (Test-Path -LiteralPath $VsmBackup) { throw "backup先が既にあります: $VsmBackup" }
if (Test-Path -LiteralPath $PilotHostBackup) { throw "backup先が既にあります: $PilotHostBackup" }
Copy-Item -LiteralPath $VsmConfig -Destination $VsmBackup -ErrorAction Stop
Copy-Item -LiteralPath $PilotHostConfig -Destination $PilotHostBackup -ErrorAction Stop
Get-FileHash -LiteralPath $VsmConfig,$VsmBackup,$PilotHostConfig,$PilotHostBackup -Algorithm SHA256
```

**期待される出力 / 状態**

同じ `$Stamp` の `.bak` が2個作成され、各バックアップのSHA-256が直前の正本と
一致する。パスとハッシュをmaintenance receiptへ記録します。

**失敗時の判断**

2個のハッシュ一致を確認できない限り設定を編集しません。片方だけ作成された場合は、
既存バックアップを上書きせず、作成済みファイルを保全してownerへ報告します。

## 3. `vsm.toml` の初回 contract 作成と追加設定

### 3.1 `[environment_contract]` を新規作成する

**実行するコマンド / 編集内容**

現行本番 `vsm.toml` にはこのセクションが無いため、「既存セクションを残して追加」
ではなく、次の3セクションをトップレベルへ新規追加します。`[environment_contract]`
は実装の閉じたフィールド集合そのものです。物理パス、WindowsのOS名、CLI executable、
`CODEX_HOME` は contract に入れず、`[environment_instance]` にだけ入れます。

```toml
[environment_contract]
supported_shells = ["powershell", "posix"]
required_endpoints = ["api.openai.com"]
workspace_writable = true
minimum_memory_mb = 4096
supported_sandboxes = ["workspace-write"]
required_sandbox = "workspace-write"
path_mapping_names = ["workspace-root"]
minimum_cli_version = "0.144.5"

[environment_contract_artifact]
store_path = "/cutover/production/environment-artifacts"
artifact_key = "codex-workspace"
artifact_version = 1

[environment_instance]
instance_id = "environment-instance:windows-native-primary"
logical_path_bindings = { workspace-root = "D:/userdata/docs/projects" }
cli_executable_path = "C:/Users/mitob/AppData/Roaming/npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
codex_home = "C:/Users/mitob/.codex"
environment_variables = {}
machine_identity = { execution = "windows-native", os = "windows" }
```

**期待される出力 / 状態**

TOMLとして解析でき、`required_sandbox` は `supported_sandboxes` に含まれ、
`path_mapping_names` は `workspace-root` だけになる。`[environment_instance]` の
binding名は contract の論理名と完全一致する。

**失敗時の判断**

`execution_location`、`machine_path`、Windows drive pathを `[environment_contract]`
へ追加してはいけません。`extra_forbid` で拒否されるため、host固有値は instanceへ戻し、
不明なフィールドを追加せずに停止して修正します。

production ComposeのAPIはhostのproduction directoryを `/cutover/production` として
読むため、TOMLのartifact storeは `/cutover/production/environment-artifacts` です。
Windows native PilotHostのpreflightは同じhost directoryを `D:/userdata/docs/projects/_cutover_20260720_fable_activation/production/environment-artifacts`
として読むため、PilotHost JSONにはWindows側の絶対パスを明示します。

### 3.2 artifact と preflight を `vsm.toml` に追加する

**実行するコマンド / 編集内容**

既存の `[production_pilot_host]` の値は残したまま、次の4項目を追加します。既存の
`work_cwd`、`work_sandbox`、candidateの `sandbox_fingerprint` は変更しません。

```toml
[production_pilot_host]
preflight_enabled = true
preflight_cli_version_file = "C:\\Users\\mitob\\AppData\\Roaming\\npm\\node_modules\\@openai\\codex\\package.json"
preflight_cache_path = "D:\\userdata\\docs\\projects\\_cutover_20260720_fable_activation\\production\\preflight-cache.json"
preflight_instance_fingerprint = "a349962aeb65f08fb8b5e5b7cebffbad573d1c595f2158cf9d2fc84de536b510"
```

`interface_pilot.environment_fingerprint` と、`routing.candidates[*].candidate` の
全candidateの `environment_fingerprint` は、次の値へ一括変更します。

```toml
environment_fingerprint = "environment-contract-sha256:9b16d4c3c11cd670696d9edb9e3aa1ff8c56450c1d08aa05099ab356436eea2b"
```

既存の `interface_pilot` の `sandbox:9c3f...`、coding候補の
`sandbox:workspace-write`、`work_cwd = "D:/userdata/docs/projects"`、CLI/model/effort
はそのままにします。

artifactを初回保存する実行環境では、contractと同じ値を使って次を実行します。

```powershell
python -c "from pathlib import Path; from vsm.environment import EnvironmentContract; from vsm.environment.artifacts import LocalEnvironmentContractStore; c=EnvironmentContract.model_validate({'supported_shells':['powershell','posix'],'required_endpoints':['api.openai.com'],'workspace_writable':True,'minimum_memory_mb':4096,'supported_sandboxes':['workspace-write'],'required_sandbox':'workspace-write','path_mapping_names':['workspace-root'],'minimum_cli_version':'0.144.5'}); a=LocalEnvironmentContractStore(Path(r'D:/userdata/docs/projects/_cutover_20260720_fable_activation/production/environment-artifacts')).save(c,artifact_key='codex-workspace',version=1); print(a.fingerprint)"
```

**期待される出力 / 状態**

`environment-contract-sha256:9b16d4c3...6eea2b` が表示され、
`environment-artifacts/environment-contract/codex-workspace/v1.json` が作成される。
同じ内容で再実行した場合だけ既存artifactをそのまま返し、別contractで同じversionを
上書きしない。

**失敗時の判断**

artifactが無い、fingerprintが一致しない、または同じversionの内容が違う場合は、
`vsm.toml`だけを先に反映せず停止します。artifactを削除・上書きせず、次のversionを
使う変更はowner/S5の再承認と新fingerprintの再計算が必要です。

### 3.3 `vsm/config.py` と設定のロードを検証する

**実行するコマンド / 編集内容**

編集した実本番設定で、まず `NaniholdConfig.model_validate` を実行します。これにより
`vsm/config.py` のstrict schema、contractとのfingerprint一致、Interface candidateの
一意性、production coding candidateの構成を確認します。

```powershell
docker compose -p eep-runbook run --no-deps --rm app python3 -c "import tomllib; from pathlib import Path; from vsm.config import NaniholdConfig; from vsm.environment import environment_fingerprint; d=tomllib.loads(Path('/workspace/config/eep-phase1-runbook.example.toml').read_text('utf-8')); c=NaniholdConfig.model_validate(d); assert c.environment_contract is not None; assert environment_fingerprint(c.environment_contract) == 'environment-contract-sha256:9b16d4c3c11cd670696d9edb9e3aa1ff8c56450c1d08aa05099ab356436eea2b'; assert c.production_pilot_host is not None and c.production_pilot_host.preflight_enabled is True; assert len([x for x in c.routing.candidates if x.candidate.adapter == 'codex-cli' and x.candidate.model_snapshot in {'gpt-5.6-luna','gpt-5.6-sol'}]) == 2; print('EEP-08 config validation: OK')"
```

このrepositoryの設定例ファイルはschema/load検証専用であり、本番のsecretや実体ファイルを
含みません。本番配備先では同じ検証を正本 `vsm.toml` の絶対パスに対して実行します。

**期待される出力 / 状態**

`EEP-08 config validation: OK` が表示される。fingerprint不一致、preflight項目不足、
coding候補の重複・欠落、Interface candidateの不一致はすべて例外になる。

**失敗時の判断**

例外を握りつぶしたり、旧fingerprintへ戻して続行しません。エラーが示すセクションだけを
修正し、3.2のfingerprint計算と3.3の検証を最初からやり直します。

## 4. `pilot-host.json` の同時反映

### 4.1 `preflight` と Windows bypass の明示設定を追加する

**実行するコマンド / 編集内容**

現行 `pilot-host.json` の `codex` に `false` を明示追加し、トップレベルに
`preflight` を追加します。API containerとWindows native PilotHostのパスが異なるため、
PilotHost側のartifact、instance、CLI version、cache、instance fingerprintを明示します。

```json
{
  "codex": {
    "candidate": {
      "adapter": "codex-cli",
      "adapter_version": "0.144.5",
      "provider": "openai",
      "selection": "exact",
      "model_snapshot": "gpt-5.6-luna",
      "effort": "xhigh",
      "toolset": [],
      "sandbox_fingerprint": "sandbox:workspace-write",
      "environment_fingerprint": "environment-contract-sha256:9b16d4c3c11cd670696d9edb9e3aa1ff8c56450c1d08aa05099ab356436eea2b"
    },
    "executable": "C:/Users/mitob/AppData/Roaming/npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe",
    "cli_version": "0.144.5",
    "working_directory_allowlist": ["D:/userdata/docs/projects"],
    "sandbox": "workspace-write",
    "mcp": { "allowlist": [], "servers": {} },
    "max_input_tokens": 50000000,
    "max_output_tokens": 2000000,
    "max_total_tokens": 52000000,
    "timeout_seconds": 1800.0,
    "win32_codex_sandbox_bypass_enabled": false
  },
  "preflight": {
    "enabled": true,
    "cli_version_file": "C:\\Users\\mitob\\AppData\\Roaming\\npm\\node_modules\\@openai\\codex\\package.json",
    "cache_path": "D:\\userdata\\docs\\projects\\_cutover_20260720_fable_activation\\production\\preflight-cache.json",
    "instance_fingerprint": "a349962aeb65f08fb8b5e5b7cebffbad573d1c595f2158cf9d2fc84de536b510",
    "environment_contract_artifact": {
      "store_path": "D:/userdata/docs/projects/_cutover_20260720_fable_activation/production/environment-artifacts",
      "artifact_key": "codex-workspace",
      "artifact_version": 1
    },
    "environment_instance": {
      "instance_id": "environment-instance:windows-native-primary",
      "logical_path_bindings": { "workspace-root": "D:/userdata/docs/projects" },
      "cli_executable_path": "C:/Users/mitob/AppData/Roaming/npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe",
      "codex_home": "C:/Users/mitob/.codex",
      "environment_variables": {},
      "machine_identity": { "execution": "windows-native", "os": "windows" }
    },
    "operational_ledger": {
      "base_url": "http://host.docker.internal:8080",
      "bearer_token_env": "LETHE_NANIHOLD_TOKEN",
      "data_space_id": "space:personal-primary",
      "timeout_seconds": 30,
      "max_page_size": 500
    }
  }
}
```

`kernel_config_path` は使用しません。API containerとWindows native PilotHostでパス名前空間が
異なるため、TOMLのcontract/artifactはAPI側、JSONのartifact/instance/CLI/cacheはWindows側
に明示します。上は `codex` と `preflight` の追加・変更部分を完全に示した例です。
`claude`、identity、receipt store等の既存トップレベルは削除せず、JSONとしてマージします。

**期待される出力 / 状態**

JSONが解析でき、PilotHost `/health` は次を返す。

```json
{
  "preflight": {
    "enabled": true,
    "instance_fingerprint": "a349962aeb65f08fb8b5e5b7cebffbad573d1c595f2158cf9d2fc84de536b510",
    "environment_fingerprint": "environment-contract-sha256:9b16d4c3c11cd670696d9edb9e3aa1ff8c56450c1d08aa05099ab356436eea2b"
  }
}
```

実際のhealthにはCLI version fileとcache pathも含まれ、`environment_fingerprint` は
Codex candidateと厳密一致する。

**失敗時の判断**

`preflight` が無い、`enabled=false`、artifact/instanceを参照できない、またはhealthの
fingerprintが違う場合はPilotHostをready扱いにしません。JSON側へ別の契約を足すのではなく、
正本の `vsm.toml` とartifactを修正します。

### 4.2 暫定 bridge が必要な場合だけ bypass を使う

**実行するコマンド / 編集内容**

コード反映と設定反映を同じmaintenance窓に完了できないという、owner/S5が承認した
短時間のbridgeに限り、`pilot-host.json` の `codex` に次を明示します。

```json
"win32_codex_sandbox_bypass_enabled": true
```

この設定でWindows nativeのCodexは `--dangerously-bypass-approvals-and-sandbox` を使います。
bridge開始時刻、終了期限、承認者、対象hostをreceiptへ記録し、preflight設定を反映したら
直ちに `false` へ戻します。

**期待される出力 / 状態**

bridge中だけ、旧 `vsm.toml` と新コードの組合せでもWindows workspace-writeが
`preflight is None` のfail-closed条件で全停止しない。bridge終了後のhealth/configには
`preflight.enabled=true` と `win32_codex_sandbox_bypass_enabled=false` が残る。

**失敗時の判断**

これはEEPの目的（契約適合をpreflightで証明してから実行）に反する一時措置です。承認、
期限、復帰確認のいずれかが無い場合は使用しません。期限を過ぎてもfalseへ戻せない場合は
Codex実行を停止し、通常のfail-closedへ戻します。bypassを恒久設定やsilent fallbackに
しません。

## 5. RouteSnapshot 再発行（必須）

contract fingerprintを変えると全candidateの `ModelCandidate.key` が変わります。既存の
`route:coding-production-20260720-luna-first` は旧candidate keyを参照するため、新設定で
そのままserveすると `active RouteSnapshot references an unregistered ModelCandidate` で
起動できません。以下を省略してはいけません。

### 5.1 旧IDのserveを一時維持する

**実行するコマンド / 編集内容**

新設定で既存serveを再作成せず、旧 `vsm.toml` と旧active IDで起動済みのserveを維持します。
新設定を実行するためのcommission用コピーだけを作ります。本番serveの停止・再起動はまだ
行いません。

```powershell
$OldSnapshotId = "route:coding-production-20260720-luna-first"
$NewSnapshotId = "route:coding-production-20260722-eep08-luna-first"
$ApiBase = "http://127.0.0.1:8000"
$PilotHostBase = "http://127.0.0.1:51872"
$S3StarActorId = "<commissioned-s3-star-actor-id>"
if ([string]::IsNullOrWhiteSpace($env:NANIHOLD_API_BEARER_TOKEN)) { throw "NANIHOLD_API_BEARER_TOKEN がありません" }
if ([string]::IsNullOrWhiteSpace($env:PILOT_HOST_BEARER_TOKEN)) { throw "PILOT_HOST_BEARER_TOKEN がありません" }
$ApiHeaders = @{
  Authorization = "Bearer $env:NANIHOLD_API_BEARER_TOKEN"
  "X-Nanihold-Device-Id" = "device:owner-web"
}
$PilotHeaders = @{
  Authorization = "Bearer $env:PILOT_HOST_BEARER_TOKEN"
  "X-Nanihold-Pilot-Host-Id" = "pilot-host:interface-primary"
  "X-Nanihold-Device-Id" = "device:interface-primary"
  "X-Nanihold-Device-Certificate-Sha256" = "ba36c850da95663816362241fde18e3e9769156247cbd45367d2f8a31bbbfc8e"
}
$TransitionConfig = Join-Path $ProductionRoot "vsm.eep08-transition.toml"
Copy-Item -LiteralPath $VsmConfig -Destination $TransitionConfig -ErrorAction Stop
# TransitionConfigには3.1〜3.2の新設定を反映し、routing.active_route_snapshot_idだけをNewSnapshotIdにする。
Select-String -LiteralPath $TransitionConfig -Pattern "active_route_snapshot_id|environment-contract-sha256:9b16d4c3|preflight_enabled = true"
```

**期待される出力 / 状態**

旧serveは旧IDでreadyのまま、新しいcommission用configは新contract、artifact、instance、
preflight、`active_route_snapshot_id = "route:coding-production-20260722-eep08-luna-first"`
を持つ。`TransitionConfig` はworktreeやgitへcommitしない一時ファイルである。

**失敗時の判断**

旧serveがreadyでない、またはTransitionConfigのfingerprint・active IDが不一致なら、
新コード/新設定のserveを起動しません。旧configのbackupを使って状態を復旧し、原因を解消します。

### 5.2 新candidate keyを確認して新snapshotをregister/approveする

**実行するコマンド / 編集内容**

まず新configでcandidate keyを出し、coding候補がLuna+Solの2件だけであることを確認します。
`vsm/config.py:256-259` の制約により、productionの
`coding_candidate_model_snapshot` に解決する候補は正確に1件、coding routeの構成は
Luna→Solの正確な2件でなければなりません。

```powershell
$ModelsJson = vsm routes models --config $TransitionConfig
$Models = $ModelsJson | ConvertFrom-Json
$LunaKey = ($Models | Where-Object { $_.candidate.model_snapshot -eq "gpt-5.6-luna" }).key
$SolKey = ($Models | Where-Object { $_.candidate.model_snapshot -eq "gpt-5.6-sol" }).key
if ([string]::IsNullOrWhiteSpace($LunaKey) -or [string]::IsNullOrWhiteSpace($SolKey)) { throw "Luna/Sol candidate keyがありません" }
if (($Models | Where-Object { $_.candidate.model_snapshot -eq "gpt-5.6-luna" }).Count -ne 1) { throw "Luna candidateが正確に1件ではありません" }
if (($Models | Where-Object { $_.candidate.model_snapshot -eq "gpt-5.6-sol" }).Count -ne 1) { throw "Sol candidateが正確に1件ではありません" }
$EvidenceCursor = (Invoke-RestMethod -Method Get -Uri "$ApiBase/api/model-registry" -Headers $ApiHeaders).evidence_cursor

vsm routes publish `
  --config $TransitionConfig `
  --route-key coding:personal-production `
  --evidence-cursor $EvidenceCursor `
  --candidate-key $LunaKey `
  --candidate-key $SolKey `
  --objective quality_max `
  --s3-star-actor-id $S3StarActorId `
  --owner-actor-id owner:primary `
  --idempotency-prefix eep08:route:20260722
```

**期待される出力 / 状態**

candidate keyの出力は、新fingerprintを含むLunaとSolをそれぞれ1件だけ返す。
`vsm routes publish` は新snapshotをregisterし、S3*とownerをapproveした後、旧snapshotが
まだPUBLISHEDならpublish段階で失敗することがある。これは期待された停止であり、
`GET /api/route-snapshots` で新IDが `OWNER_APPROVED`、旧IDが `PUBLISHED` であることを
確認します。

**失敗時の判断**

register/approve前の失敗なら、evidence cursor、candidate key、artifact、configを直して停止します。
新IDが既にregister済みなら、`vsm routes publish` 全体を再実行しません。registerの再実行は
`RouteSnapshot already exists` で弾かれるため、次のAPI操作へ進みます。新IDが存在しない
場合に限り、payloadとエラーを保存してowner/S5判断を得ます。

### 5.3 APIで旧snapshotをRETIREする

**実行するコマンド / 編集内容**

`$ApiBase`、`$ApiHeaders`、`$PilotHeaders`、`$S3StarActorId` はcommission済みの値として
事前に設定します。
次のAPI操作は旧snapshotを指定し、後継新IDを明示します。

```powershell
$RetireBody = @{
  reason_code = "superseded_by_approved_snapshot"
  replacement_snapshot_id = $NewSnapshotId
  actor_id = "owner:primary"
  idempotency_key = "eep08:route:20260722:retire-old"
} | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri "$ApiBase/api/route-snapshots/$OldSnapshotId/retirements" `
  -Headers $ApiHeaders -ContentType "application/json" -Body $RetireBody
```

**期待される出力 / 状態**

旧snapshotが `RETIRED`、新snapshotが `OWNER_APPROVED` のままになる。retirementは新snapshot
を暗黙publishしないため、この2操作の間は同じrouteにPUBLISHEDが無い。

**失敗時の判断**

409なら、新snapshotのroute key、evidence cursor、candidate keyが現行registryと一致するかを
再確認します。旧snapshotが既にRETIRE済みなら同じidempotency keyの結果をread-backし、
retirementを別の理由で二重実行しません。

### 5.4 APIで新snapshotをPUBLISHする

**実行するコマンド / 編集内容**

```powershell
$PublishBody = @{
  actor_id = "owner:primary"
  idempotency_key = "eep08:route:20260722:publish-new"
} | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri "$ApiBase/api/route-snapshots/$NewSnapshotId/publish" `
  -Headers $ApiHeaders -ContentType "application/json" -Body $PublishBody
```

**期待される出力 / 状態**

`GET /api/route-snapshots` で、新IDが `PUBLISHED`、旧IDが `RETIRED`、新snapshotの
candidate keysが `[LunaKey, SolKey]` の順、evidence cursorが現在値と一致する。

**失敗時の判断**

旧snapshotがまだPUBLISHEDなら5.3を完了させます。`already exists` はregister再実行の
合図ではありません。candidate/evidence/approvalが一つでも不足する場合はpublishせず、
不足したstateから継続します。

### 5.5 active IDを新IDへ変更しserveを再作成する

**実行するコマンド / 編集内容**

5.4のread-backを保存してから、正本 `vsm.toml` の次の1行だけを新IDへ変更します。

```toml
[routing]
active_route_snapshot_id = "route:coding-production-20260722-eep08-luna-first"
```

新configを配置し、supervisorの通常手順で `PilotHost → API` の順に再作成します。
`TransitionConfig` は成功確認後に保全または削除します。

**期待される出力 / 状態**

serve起動時のactive snapshot検証が通り、`GET /api/route-snapshots` の新IDがPUBLISHED、
PilotHost healthのcandidateが新fingerprint、Codex実行前healthのpreflightがenabledになる。

**失敗時の判断**

`active RouteSnapshot references an unregistered ModelCandidate`、stale evidence、
preflight mismatchのいずれかならdispatchを再開しません。直前backupとAPI stateを保全し、
5.2〜5.4の順序を飛ばしていないか確認します。

## 6. 起動・preflight・最小実行の確認

### 6.1 artifact、config、PilotHostを順序どおり確認する

**実行するコマンド / 編集内容**

次の順で実行します。

```powershell
# 1. immutable artifactのread-back
python -c "from pathlib import Path; from vsm.environment.artifacts import LocalEnvironmentContractStore; a=LocalEnvironmentContractStore(Path(r'D:/userdata/docs/projects/_cutover_20260720_fable_activation/production/environment-artifacts')).get(artifact_key='codex-workspace',version=1); print(a.fingerprint)"
# 2. vsm.tomlの3.3検証コマンドを再実行
# 3. pilot-host.jsonを配置してJSON parseを確認
Get-Content -Raw -LiteralPath $PilotHostConfig | ConvertFrom-Json | Out-Null
# 4. 既存supervisorの停止・起動手順でPilotHostを再作成し、認証付きhealthを取得
Invoke-RestMethod -Method Get -Uri "$PilotHostBase/health" -Headers $PilotHeaders
# 5. 既存supervisorの手順でNanihold APIを再作成しreadyを確認
Invoke-RestMethod -Method Get -Uri "$ApiBase/health/ready"
```

**期待される出力 / 状態**

artifact、TOML、PilotHost health、API readyが同じcontract/instance fingerprintを示す。
API起動時にLETHE、active RouteSnapshot、PilotHost health、EnvironmentInstance bindingが
検証され、旧candidate keyを参照するエラーが無い。

**失敗時の判断**

一つでもread-backが違えば、稼働中processをready扱いにせず、最小dispatchも行いません。
secretを再発行する、endpointを変える、bypassをtrueにすることで進めず、該当artifact/config/
healthの差分を保全します。

### 6.2 preflight gateの設定をread-backする

**実行するコマンド / 編集内容**

現行実装にはpreflight専用の独立APIはありません。API ready後、まずPilotHost healthで
preflight gateの設定だけをread-backし、実行証拠を作る操作は6.3のowner承認済み最小
WorkItem dispatchに限定します。存在しないno-op endpointや架空のpayloadは作りません。

```powershell
$Health = Invoke-RestMethod -Method Get -Uri "$PilotHostBase/health" -Headers $PilotHeaders
if ($Health.preflight.enabled -ne $true) { throw "PilotHost preflightがenabledではありません" }
if ($Health.preflight.environment_fingerprint -ne "environment-contract-sha256:9b16d4c3c11cd670696d9edb9e3aa1ff8c56450c1d08aa05099ab356436eea2b") { throw "PilotHost contract fingerprintが不一致です" }
```

**期待される出力 / 状態**

healthの `preflight.enabled=true`、contract/instance fingerprint、CLI version file、cache
pathがTOMLの期待値と一致する。実際のpreflight evidenceは次の6.3のdispatchで生成する。

**失敗時の判断**

preflight失敗を成功扱いにせず、cacheを削除して無視もしません。証拠のinstance fingerprint、
contract fingerprint、CLI version/mtime、sandbox policyを保存し、原因を直して6.3の
最小dispatchを再実行します。

### 6.3 最小 WorkItem を1件だけdispatchする

**実行するコマンド / 編集内容**

ownerが承認した最小の既存WorkItem IDだけを `$ApprovedWorkItemId` に設定し、正規の
dispatch APIを1件だけ実行します。`DispatchWorkItemRequest` はactorとidempotency keyだけを
要求します。

```powershell
$ApprovedWorkItemId = "<owner-approved-minimum-work-item-id>"
$DispatchBody = @{
  actor_id = "owner:primary"
  idempotency_key = "eep08:smoke:20260722"
} | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri "$ApiBase/api/work-items/$ApprovedWorkItemId/dispatches" `
  -Headers $ApiHeaders -ContentType "application/json" -Body $DispatchBody
# dispatch後、既存のexecution/receipt read-only手順でpreflight evidenceとreceiptを照合する。
```

**期待される出力 / 状態**

Codexが通常の `--sandbox workspace-write` で起動し、Pilot receipt、actual model、
preflight evidence、Ledger eventが保存される。Lunaから開始し、Solは明示的なcoding
escalation条件のときだけ使われる。

**失敗時の判断**

receiptがunknown、actual modelが要求と違う、sandboxがbypass、preflight証拠が無い場合は
成功と推定しません。対象Executionを停止し、外部状態を照合してからownerへ報告します。

## 7. Dockerでの設定検証と既存テスト

### 7.1 設定例の検証方法を保存する

**実行するコマンド / 編集内容**

3.3のDockerコマンドをmaintenance receiptへ保存します。この検証は
`NaniholdConfig.model_validate` を使うため、`vsm/config.py` の実装と同じstrict schemaで
契約・candidate・preflight関連の不変条件を検査できます。artifact実体・secret・Windows
pathの存在確認は配備先で別途行います。

**期待される出力 / 状態**

`EEP-08 config validation: OK` があり、既存の本番設定のfingerprint宣言が新contract値に
一括更新されている。

**失敗時の判断**

Docker検証ができない場合は「検証済み」と報告しません。Docker/Composeの状態を直してから
再実行します。

### 7.2 既存テストを全件実行する

**実行するコマンド / 編集内容**

このworktreeで次を実行します。`app` imageが既にある場合も `--no-deps` で不要なserviceを
起動せず、Composeの `eep-runbook` projectに隔離します。

```powershell
wsl.exe --cd /mnt/d/userdata/docs/projects/_eep_worktrees/integration -- bash -lc 'docker compose -p eep-runbook run --no-deps --rm app python3 -m pytest'
```

**期待される出力 / 状態**

pytestがexit code 0で全件passする。失敗0件、error0件をreceiptへ記録します。

**失敗時の判断**

既存テストの失敗を手順書の変更で隠しません。テストログを保全し、コードを変更せずに
原因を切り分けます。今回の配備が完了したとは報告しません。

## 8. ロールバック

### 8.1 設定・processを停止してbackupへ戻す

**実行するコマンド / 編集内容**

新設定で新規dispatchを停止し、進行中receiptを成功と推定せずreconciliationします。
API、PilotHostを停止した後、2.1で作成した同一timestampのbackupを復元します。

```powershell
if (!(Test-Path -LiteralPath $VsmBackup -PathType Leaf)) { throw "vsm backupがありません" }
if (!(Test-Path -LiteralPath $PilotHostBackup -PathType Leaf)) { throw "pilot-host backupがありません" }
Copy-Item -LiteralPath $VsmBackup -Destination $VsmConfig -Force -ErrorAction Stop
Copy-Item -LiteralPath $PilotHostBackup -Destination $PilotHostConfig -Force -ErrorAction Stop
Get-FileHash -LiteralPath $VsmConfig,$VsmBackup,$PilotHostConfig,$PilotHostBackup -Algorithm SHA256
```

**期待される出力 / 状態**

復元後の正本とbackupのSHA-256が一致し、旧設定のactive ID・旧fingerprint・旧preflight状態が
確認できる。

**失敗時の判断**

ハッシュが一致しない場合はserveを起動しません。backupを上書きせず、receiptと実ファイルを
保全してownerへ報告します。

### 8.2 RouteSnapshotのstateを先に整合させる

**実行するコマンド / 編集内容**

新snapshotをPUBLISHEDにした後のrollbackは、ファイル復元だけでは完了しません。旧candidate
registryを含む旧configをcommission用に戻し、APIで新snapshotを
`superseded_by_approved_snapshot` としてRETIREし、旧snapshotを同じregister/approve/publish
手順で復旧してから旧active IDでserveを再作成します。

**期待される出力 / 状態**

新snapshotがRETIRED、旧snapshotがPUBLISHED、正本configのactive IDとPUBLISHED snapshotが
一致する。

**失敗時の判断**

旧candidateが新configに無い状態でRouteSnapshot操作を混ぜません。stateが不明ならdispatchを
再開せず、APIのsnapshot一覧、Ledger、backupをowner/S5が照合します。暗黙publish、旧IDの
再利用、candidate fingerprintの手書き復元はしません。

### 8.3 旧順序で再起動・確認する

**実行するコマンド / 編集内容**

旧PilotHost → 旧APIの順で再起動し、6.1のhealth/readiness、6.2のpreflight、6.3の最小
WorkItemを、ownerの再承認後にやり直します。

**期待される出力 / 状態**

旧fingerprint・旧candidate key・旧active snapshotが一致し、receipt reconciliationが通る。

**失敗時の判断**

旧設定でも一致しない場合は、fallbackで別routeや別modelへ切り替えず停止します。

## 9. 完了チェックリスト

- [ ] 2個の日時付きbackupとSHA-256がmaintenance receiptにある
- [ ] 初回 `[environment_contract]` の8フィールドが実装スキーマと一致する
- [ ] Windows nativeの `powershell`、`workspace-root`、workspace、CLI 0.144.5を実測した
- [ ] contract fingerprintがartifact、TOML、全candidate、PilotHost healthで一致する
- [ ] instance fingerprintがbinding、TOML、PilotHost healthで一致する
- [ ] `preflight_enabled=true` とPilotHost `preflight.enabled=true` が一致する
- [ ] `win32_codex_sandbox_bypass_enabled=false` が通常状態で明示される
- [ ] bypass=trueを使った場合、承認・期限・false復帰のreceiptがある
- [ ] coding candidateはLunaとSolの正確な2件で、RouteSnapshot順序もLuna→Solである
- [ ] 新RouteSnapshotがregister、S3* approve、owner approve済みである
- [ ] 旧RouteSnapshotをAPIでRETIREしてから新RouteSnapshotをAPIでPUBLISHした
- [ ] `routing.active_route_snapshot_id` と新PUBLISHED snapshotが一致する
- [ ] preflight no-opの証拠、cache、`environment_instance_preflight_verified` Eventがある
- [ ] 通常sandboxで最小WorkItem、Pilot receipt、Ledger eventを確認した
- [ ] Docker設定検証がOKで、既存pytestが全件passした

契約・調達境界の承認はowner/S5、実体の発見・構築・preflight実測・Windows bypassの撤去・
RouteSnapshotのowner操作は担当者が実施します。本実装はそれらを自動実行せず、設定・証拠・
fail-fast境界を接続しただけです。
