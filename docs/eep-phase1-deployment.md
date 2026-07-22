# EEP Phase 1 配備手順

この手順は、EnvironmentContract / EnvironmentInstance / preflight を本番へ反映する
ための手動手順です。本 worktree から本番設定を直接変更したり、PilotHost/API の本番
process を操作したりしません。設定値はcommission済みの実体・契約・DataSpaceへ置き換えて
ください。

## 1. 反映前の準備

1. オーナー/S5 が EnvironmentContract と調達境界を承認する。
2. Nagi/S3 が実行実体を用意し、workspace、Codex executable、`CODEX_HOME`、必要な
   endpoint、メモリ、POSIX shell を実測する。
3. 契約をローカル版付きストアへ、同じ fingerprint のまま保存する。
4. `environment_instance.instance_fingerprint` は手計算で作らず、binding から実装が
  計算する値を commissioning receipt と照合する。
5. WSL 実体で通常の `--sandbox workspace-write` が preflight を通ることを確認する。
   確認前に Windows native の既定運用へ切り替えない。

## 2. `vsm.toml` の追加差分

既存の `[environment_contract]` と `[production_pilot_host]` は正本として残し、次を
追加します。値は例です。

```diff
 [production_pilot_host]
 preflight_enabled = true
 preflight_cli_version_file = "/usr/local/lib/node_modules/@openai/codex/package.json"
 preflight_cache_path = "/var/lib/nanihold-pilot/preflight-cache.json"
 preflight_instance_fingerprint = "<commissioned-instance-sha256>"

+[environment_contract_artifact]
+store_path = "/var/lib/nanihold-pilot/environment-artifacts"
+artifact_key = "codex-workspace"
+artifact_version = 1
+
+[environment_instance]
+instance_id = "environment-instance:primary"
+logical_path_bindings = { workspace-root = "/workspace" }
+cli_executable_path = "/usr/local/bin/codex"
+codex_home = "/var/lib/nanihold-pilot/codex-home"
+environment_variables = {}
+machine_identity = { host = "<commissioned-host>" }
```

Runtime は `environment_contract_artifact` を `LocalEnvironmentContractStore` から取得し、
`environment_contract` と完全一致しなければ停止します。`environment_instance` から
実体 fingerprint を計算し、`preflight_instance_fingerprint` と一致しなければ停止します。
この差分を入れた `vsm.toml` は PilotHost 側から `kernel_config_path` で参照させます。

## 3. `pilot-host.json` の追加差分

PilotHost JSON の preflight は、Kernel TOML を参照できない場合の明示的 fallback です。
`kernel_config_path` がある場合は Kernel TOML の値が優先されます。

```diff
   "preflight": {
     "enabled": true,
+    "kernel_config_path": "/etc/nanihold/vsm.toml",
     "cli_version_file": "/usr/local/lib/node_modules/@openai/codex/package.json",
     "cache_path": "/var/lib/nanihold-pilot/preflight-cache.json",
     "instance_fingerprint": "<commissioned-instance-sha256>",
     "environment_contract": { "...": "Kernel と同じ契約のfallback" },
+    "environment_contract_artifact": {
+      "store_path": "/var/lib/nanihold-pilot/environment-artifacts",
+      "artifact_key": "codex-workspace",
+      "artifact_version": 1
+    },
+    "data_space_id": "space:personal-primary",
+    "environment_instance": {
+      "instance_id": "environment-instance:primary",
+      "logical_path_bindings": { "workspace-root": "/workspace" },
+      "cli_executable_path": "/usr/local/bin/codex",
+      "codex_home": "/var/lib/nanihold-pilot/codex-home",
+      "environment_variables": {},
+      "machine_identity": { "host": "<commissioned-host>" }
+    },
+    "operational_ledger": {
+      "base_url": "https://lethe.example.invalid",
+      "bearer_token_env": "LETHE_BEARER_TOKEN",
+      "data_space_id": "space:personal-primary",
+      "timeout_seconds": 30,
+      "max_page_size": 100
+    }
   }
```

Codex の暫定 Windows 経路を明示的に許可する場合だけ、`codex` に次を追加します。

```diff
   "codex": {
+    "win32_codex_sandbox_bypass_enabled": false,
```

既定値は `false` です。`false` では Windows でも通常の `--sandbox workspace-write` を
使い、成功済み preflight がない実行は拒否します。`true` は
`--dangerously-bypass-approvals-and-sandbox` を使う一時経路であり、WSL 切替後に削除します。

## 4. 起動・再起動の順序

停止・起動は本番 supervisor の手順に従い、次の順序を守ります。

1. 新しい契約 artifact を immutable に配置し、read-back して fingerprint と version を確認する。
2. `vsm.toml` を配置し、Kernel の設定検証を実行する。
3. `pilot-host.json` を配置し、`kernel_config_path`、証明書、secret 名、artifact selector を確認する。
4. 既存の PilotHost を停止する。
5. PilotHost を起動し、認証付き `/health` が `ready`、候補 fingerprint、coding candidate、
   endpoint を返すことを確認する。
6. Nanihold API を再起動する。API は起動時に LETHE、RouteSnapshot、PilotHost health、
   EnvironmentInstance binding を検証する。
7. API の ready を確認してから、preflight 専用の no-op dispatch を 1 回行う。
8. preflight evidence の `environment_instance_verified` Event、cache、receipt を確認する。
9. 最小 WorkItem を dispatch し、通常 sandbox、workspace write、Pilot receipt、Ledger
   event を確認する。

## 5. ロールバック

1. 新設定で新規 dispatch を停止し、進行中 receipt は成功と推定せず reconciliation する。
2. API を停止する。
3. PilotHost を停止する。
4. 直前の `vsm.toml`、`pilot-host.json`、artifact version selector を復元する。
   immutable artifact 自体は削除せず、selector を戻す。
5. PilotHost → API の順に再起動し、旧 fingerprint と RouteSnapshot の一致を確認する。
6. 失敗した preflight cache はコピーして保全した後、旧 fingerprint に一致する cache だけを使う。
   壊れた cache の無視や自動修復で起動を継続しない。
7. reconciliation 完了、owner への報告、rollback receipt の記録後に dispatch を再開する。

## 6. 検証チェックリスト

- [ ] 契約 artifact の key/version/read-back/fingerprint が一致する
- [ ] `vsm.toml` の契約と artifact が一致する
- [ ] candidate、PilotHost、artifact の `environment_fingerprint` が一致する
- [ ] `EnvironmentInstance` の logical path、CLI、`CODEX_HOME` が実在し、fingerprint が一致する
- [ ] `preflight_instance_fingerprint` と実体 fingerprint が一致する
- [ ] preflight が要求 sandbox、workspace write、endpoint、memory、shell、path mapping を実測する
- [ ] `environment_instance_verified` Event に instance fingerprint と証拠がある
- [ ] 同じ verification tuple の 2 回目は cache hit になり、Codex 試走を繰り返さない
- [ ] CLI version/mtime または契約が変わったときだけ再試走する
- [ ] Windows native で `win32_codex_sandbox_bypass_enabled=false` が通常 sandbox を使う
- [ ] WSL 実体で preflight 合格を確認するまで既定 coding 実体を切り替えない
- [ ] API/PilotHost の再起動後も認証付き health と receipt reconciliation が通る

## 残る手動ステップ

契約・調達境界の承認はオーナー/S5、実体の発見・構築・preflight 実測・WSL 切替・
Windows bypass の撤去は Nagi/S3 が実施します。本実装はそれらを自動で実行せず、
設定・証拠・fail-fast 境界を接続しただけです。
