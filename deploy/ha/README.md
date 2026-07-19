# Nanihold / LETHE PC内HA配備

このディレクトリは、既存のHyper-V Internal switch `mcp-internal`上に
NaniholdとLETHEを別々の3-node k3s clusterとして配備するための正本です。
構成生成、VM作成、cluster bootstrap、検証、障害測定、backup、restoreを
明示的なreceiptで接続します。旧経路、環境変数からの既定値、SQLite fallback、
実行中の二重書きはありません。

## 現在の状態と起動を止めているもの

2026-07-20時点では、設計・スクリプト・静的試験までが対象であり、
VM作成、network変更、NAS書込、k3s導入は実行していません。次の実値がないため
`preflight.ps1`は意図的にfail-fastします。

- 現在の作業sessionはHyper-Vを変更できる管理者sessionではない
- 書込とdurable flushを確認できるNAS backup targetが未指定・未到達
- Ubuntu Server ISOの絶対pathとSHA-256が未指定
- cluster専用SSH公開鍵、秘密鍵、固定済み`known_hosts`が未指定
- digest固定したk3s binary／installer、operator manifest、container imageを含む
  deployment inputが未作成
- ACLをowner限定したsecret inputが未作成
- 現在のLETHE imageにはHA用canary、restore-state、分離projection process、
  canonical backup/restore image契約がまだ存在しない
- 現在のPilotHost transportはHTTP active/standbyであり、計画にある外向き
  resumable stream契約は未実装

`Read-DeploymentInput`はruntime contract receiptのdigestだけでなく内容も検証し、
上記capabilityがsource commitとtest receiptで`implemented-verified`にならない限り
`RUNTIME_CONTRACT_UNAVAILABLE`で停止します。このdirectoryは現時点では配備可能と
みなしてはいけません。

これらを推測値で補完してはいけません。すべて用意した後、管理者PowerShell 7
から以下の手順を先頭から実行します。

## 固定トポロジ

| 障害領域 | Node / VIP | Address | Memory |
|---|---|---:|---:|
| 既存MCP Gateway | 既存VM | `172.31.100.10` | 既存 |
| Nanihold | VIP | `172.31.100.20` | - |
| Nanihold | `nh-control-a` | `172.31.100.21` | 8 GiB |
| Nanihold | `nh-control-b` | `172.31.100.22` | 8 GiB |
| Nanihold | `nh-control-q` | `172.31.100.23` | 4 GiB |
| LETHE | VIP | `172.31.100.30` | - |
| LETHE | `lethe-a` | `172.31.100.31` | 16 GiB |
| LETHE | `lethe-b` | `172.31.100.32` | 16 GiB |
| LETHE | `lethe-q` | `172.31.100.33` | 8 GiB |

`topology.psd1`以外のaddressやnodeを受け付けません。NaniholdとLETHEはpod
CIDR、service CIDR、k3s token、kubeconfig、VMを共有しません。quorum nodeは
application workloadからtaintし、2台のdata/control nodeへreplicaを分散します。
VIPはkube-vipで提供し、既存Gateway `.10`は変更しません。

LETHEは3-instance CloudNativePGを`remote_apply`、同期replica 1以上で動かし、
Longhorn volumeを3 replicaにします。Event Ledger、content-addressed blob、
Projectionは同じPersonal DataSpaceを明示的に使用します。NaniholdはLETHE VIP
だけを参照します。

PilotHostは`nh-control-a`と`nh-control-b`へ1台ずつ固定し、Fable candidateは
`claude-fable-5 / high`、coding S1はdeployment inputの明示model / `xhigh`
です。MCPはtyped allowlistにある`history`と既存Gateway toolだけをconfigへ
生成します。credentialはmanifest、ConfigMap、command lineへ書かず、
Kubernetes Secretを標準入力から投入します。

## 入力契約

`contracts/deployment-input.schema.json`と`contracts/secret-input.schema.json`
に厳密一致する2つのJSONを、repository外のowner限定directoryへ作成します。
余分なfield、空値、placeholder、tagだけのimage、hash不一致を受け付けません。
secretは各32文字以上で、Windows ACLに`Everyone`、`BUILTIN\Users`、
`Authenticated Users`のallow entryがないことが必要です。

deployment inputには次の実体を固定します。

- Ubuntu ISO SHA-256
- k3s version、local installer、local binaryと各SHA-256
- `oscdimg.exe`、Longhorn、CloudNativePG、monitoring manifestと各SHA-256
- kube-vip、Nanihold、LETHE、PilotHost、backup imageのdigest
- backup image OCI archiveの絶対pathとSHA-256。digest表記だけでは受理しない
- Claude/Codex CLI version、model、effort、budget、timeout
- SandboxProfile証明digestまたはclassifierを使う明示mode
- MCP Gateway URL `https://172.31.100.10/...`とtyped tool allowlist
- 2つのPilotHost/device identityと配置node
- Personal DataSpace ID、storage容量、NAS NFS server/export

## 配備手順

以下の例のpathは説明用です。実在し、hashとACLを検証済みのpathに置き換えます。
receiptと生成先は既存であれば停止するため、上書きされません。

### 1. 読み取りと耐久書込のpreflight

```powershell
.\preflight.ps1 `
  -DeploymentInputPath C:\secure\nanihold\deployment.json `
  -SecretInputPath C:\secure\nanihold\secrets.json `
  -UbuntuIsoPath D:\ISO\ubuntu-server.iso `
  -SshPublicKeyPath C:\secure\nanihold\id_cluster.pub `
  -NasBackupPath \\nas\nanihold-backup `
  -OutputReceiptPath C:\secure\nanihold\preflight.json
```

この段階はVMを作りません。管理者権限、Hyper-V command、switch、`.10`
Gateway到達性、ISO／artifact digest、SSH鍵、IP／VM名競合、memory／disk、
NASへのCreateNew・flush・deleteをすべて確認します。

### 2. cloud-initとmanifestの決定論的生成

```powershell
.\render-cloud-init.ps1 `
  -DeploymentInputPath C:\secure\nanihold\deployment.json `
  -SshPublicKeyPath C:\secure\nanihold\id_cluster.pub `
  -OutputDirectory C:\secure\nanihold\seed

.\render-manifests.ps1 `
  -DeploymentInputPath C:\secure\nanihold\deployment.json `
  -OutputDirectory C:\secure\nanihold\manifests
```

生成物にsecret値や未解決placeholderがあれば停止します。

### 3. VM作成とcluster bootstrap

まず`Plan`で同じ入力・receiptを検証し、出力にmutationがないことを確認します。
`Apply`だけがVMとclusterを変更します。

```powershell
.\provision.ps1 `
  -Mode Plan `
  -DeploymentInputPath C:\secure\nanihold\deployment.json `
  -PreflightReceiptPath C:\secure\nanihold\preflight.json `
  -UbuntuIsoPath D:\ISO\ubuntu-server.iso `
  -SeedIsoDirectory C:\secure\nanihold\seed `
  -OutputReceiptPath C:\secure\nanihold\provision.json

.\provision.ps1 `
  -Mode Apply `
  -DeploymentInputPath C:\secure\nanihold\deployment.json `
  -PreflightReceiptPath C:\secure\nanihold\preflight.json `
  -UbuntuIsoPath D:\ISO\ubuntu-server.iso `
  -SeedIsoDirectory C:\secure\nanihold\seed `
  -OutputReceiptPath C:\secure\nanihold\provision.json

.\bootstrap.ps1 `
  -Mode Plan `
  -DeploymentInputPath C:\secure\nanihold\deployment.json `
  -SecretInputPath C:\secure\nanihold\secrets.json `
  -PreflightReceiptPath C:\secure\nanihold\preflight.json `
  -ProvisionReceiptPath C:\secure\nanihold\provision.json `
  -RenderedManifestDirectory C:\secure\nanihold\manifests `
  -SshPrivateKeyPath C:\secure\nanihold\id_cluster `
  -SshPublicKeyPath C:\secure\nanihold\id_cluster.pub `
  -KnownHostsPath C:\secure\nanihold\known_hosts `
  -KubeconfigOutputDirectory C:\secure\nanihold\kubeconfig `
  -OutputReceiptPath C:\secure\nanihold\bootstrap.json
```

Plan通過後、同じ引数の`-Mode Apply`を明示します。bootstrapはlocalでdigestを
検証したk3sだけを各VMへ転送し、2 clusterを別tokenで初期化します。operator、
secret、workloadを順序付きで適用し、digestを含むreceiptを発行します。
途中状態を別backendへfallbackして継続しません。

### 4. 稼働検証

```powershell
.\verify.ps1 `
  -Mode Live `
  -DeploymentInputPath C:\secure\nanihold\deployment.json `
  -SecretInputPath C:\secure\nanihold\secrets.json `
  -BootstrapReceiptPath C:\secure\nanihold\bootstrap.json `
  -NaniholdKubeconfigPath C:\secure\nanihold\kubeconfig\nanihold.yaml `
  -LetheKubeconfigPath C:\secure\nanihold\kubeconfig\lethe.yaml `
  -NasBackupPath \\nas\nanihold-backup `
  -OutputReceiptPath C:\secure\nanihold\verified.json
```

3+3 node、Nanihold 2 replica、PilotHost 2 instance、LETHE API／Projection、
PostgreSQL 3 instance、`remote_apply`と同期replica、両VIP、既存Gateway、
NAS書込を検査します。さらに実際のbackup imageを一時Jobで起動し、
`contract --format json`が`backup`、`restore`、
`canonical-event-blob-projection-postgres`、空target必須を正確に宣言することを
確認します。digestだけ存在しCLIが未実装のimageは`verified`になりません。

### 5. RPO 0 / RTO 5分の受入測定

`measure-failover.ps1`はLETHEへ64-byte canaryを原子的に書き、Event、blob、
Projectionの3箇所で一致を確認してから、選択した1 memberだけを中断します。
回復後に同じcanaryのcursorとdigestを再照合し、3回連続readyまでの時間が
300秒以内の場合だけreceiptを発行します。pollingは1秒間隔のHTTPロジックで、
モデルを呼びません。

```powershell
.\measure-failover.ps1 `
  -Mode Apply `
  -RiskAcceptance INTERRUPT_ONE_VERIFIED_HA_MEMBER `
  -Service Lethe `
  -FaultKind VM `
  -Target lethe-a `
  -DeploymentInputPath C:\secure\nanihold\deployment.json `
  -SecretInputPath C:\secure\nanihold\secrets.json `
  -VerificationReceiptPath C:\secure\nanihold\verified.json `
  -NaniholdKubeconfigPath C:\secure\nanihold\kubeconfig\nanihold.yaml `
  -LetheKubeconfigPath C:\secure\nanihold\kubeconfig\lethe.yaml `
  -OutputReceiptPath C:\secure\nanihold\failover-lethe-a.json
```

VM障害は管理者権限で対象1台をhard power cycleします。Pod障害は所属clusterと
許可済みapp labelを検証してから対象1 podだけを削除します。NaniholdとLETHEの
少なくとも各1 memberで測定receiptを残します。構成値だけでは受入完了にしません。

この測定にはLETHE imageが次の厳密APIを実装している必要があります。

- `POST /api/ha/canaries`
- `GET /api/ha/canaries/{canary_id}`
- Event cursor、blob SHA-256、Projection cursorを同じ応答で証明すること

APIが未実装または応答契約が異なる場合は測定を開始せず、LETHE実装を先に直します。

## Backupとrestore

bootstrapはhash検証済みOCI archiveを3台すべてのLETHE nodeへimportし、
宣言したimage digestがnode内に存在しなければ停止します。`backup.ps1`は
Live検証済みclusterのcanonical backup Jobを1回だけ開始し、NAS上に
新規directoryがちょうど1つ増えたことを要求します。manifestはEvent export、
blob manifest、Projection cursor、PostgreSQL backup、signature digestを
すべて含み、Projection cursorとEvent cursorが一致しなければ失敗します。

```powershell
.\backup.ps1 `
  -Mode Apply `
  -DeploymentInputPath C:\secure\nanihold\deployment.json `
  -SecretInputPath C:\secure\nanihold\secrets.json `
  -VerificationReceiptPath C:\secure\nanihold\verified.json `
  -LetheKubeconfigPath C:\secure\nanihold\kubeconfig\lethe.yaml `
  -NasBackupPath \\nas\nanihold-backup `
  -TimeoutSeconds 1800 `
  -OutputReceiptPath C:\secure\nanihold\backup.json
```

restore先は別の空LETHE clusterでなければなりません。先にread-onlyの
`empty-target.ps1`を実行し、Event／blob／Projectionの全countとcursorが0で
あるreceiptを作ります。

```powershell
.\empty-target.ps1 `
  -DeploymentInputPath C:\secure\nanihold\deployment.json `
  -SecretInputPath C:\secure\nanihold\secrets.json `
  -LetheKubeconfigPath C:\secure\nanihold\kubeconfig\empty-lethe.yaml `
  -OutputReceiptPath C:\secure\nanihold\empty-target.json

.\restore.ps1 `
  -Mode Apply `
  -RiskAcceptance RESTORE_ONLY_INTO_VERIFIED_EMPTY_LETHE `
  -DeploymentInputPath C:\secure\nanihold\deployment.json `
  -SecretInputPath C:\secure\nanihold\secrets.json `
  -BackupReceiptPath C:\secure\nanihold\backup.json `
  -EmptyTargetReceiptPath C:\secure\nanihold\empty-target.json `
  -LetheKubeconfigPath C:\secure\nanihold\kubeconfig\empty-lethe.yaml `
  -NasBackupPath \\nas\nanihold-backup `
  -TimeoutSeconds 3600 `
  -OutputReceiptPath C:\secure\nanihold\restore.json
```

restore後は`/api/restore/state`のDataSpace、Event cursor/count、blob count、
Projection cursorをbackup receiptと完全一致させます。NASが読めない、manifest
がNAS root外、digest不一致、targetが空でない場合はJobを作りません。

## 監視と障害境界

monitoring ruleはNanihold／LETHE VIP、PostgreSQL同期replica、Projection lag、
PilotHostの`transport_unknown`、24時間以上検証backupがない状態を通知します。
status、health、failover測定はProjectionと決定論的ロジックだけを使い、
Fable、Opus、その他のmodelを呼びません。

- service、container、単一VM障害: 受入目標 RPO 0、RTO 300秒
- 物理PC、Hyper-V host、`mcp-internal` switch、host電源障害: 今回のHA対象外
- 物理PC障害後の復旧: NAS backupからの災害復旧。別途restore時間を記録する

RPO 0は同一PC上の同期replicaに対する目標です。物理PC全損に対するRPO 0を意味
しません。NAS backupは物理障害からの復旧可能性を提供しますが、最後に検証した
backup以降のデータは失われ得ます。この境界をUIと運用報告で隠してはいけません。
