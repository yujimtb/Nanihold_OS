# Local verification

## 目的

この環境は、production の Fable 設定を変えずに、WebUI から LETHE、Nanihold API、外部 PilotHost、実 Claude Code CLI までをローカルで確認するためのものです。

検証候補は起動時にインストール済み Claude Code の版を固定し、`claude-haiku-4-5-20251001 / low`、`observe_only`、tools disabled として登録します。Fable と Opus、write Effect、permission classifier、model fallback は使いません。一回の Interface turn の上限は USD 0.05 です。

## 前提

- Windows 上で `claude.cmd` が認証済みである
- `python`、`pwsh`、Windows Docker DesktopのDocker Composeが使える
- Nanihold OS と同じ親ディレクトリに LETHE repository `skcollege_database` がある
- ローカルの指定 port を loopback で利用できる

設定、token、SQLite、ログ、Pilot workspace は `.local-verification/` に生成され、GitとDocker build contextから除外されます。未追跡の設計資料`docs/archive/`もbuild contextへ送りません。初期化は既存ディレクトリを上書きしません。

## 最短手順

repository root の PowerShell で実行します。

```powershell
.\local-review.cmd init
.\local-review.cmd up
```

`up` の最後に表示される `http://localhost:<動的port>` を開きます。WebUI が求める Bearer token は別コマンドで表示します。

```powershell
.\local-review.cmd token
```

Conversation 画面の `conversation:local-verification` へメッセージを送ると、その時だけ実モデルを一回呼びます。owner message は呼出し前に personal local Lake へ保存され、応答と provider session ID も Event Ledger に残ります。

Conversation上部には実際のcandidate名とeffortを表示します。応答後はそのturnのinput、cache creation input、cache read input、output token、USD費用を表示し、同じusageをEvent Ledgerからdrill-downできます。

CLI から一回だけ疎通する場合:

```powershell
.\local-review.cmd smoke
```

`smoke` は毎回新しい実モデル呼出しです。費用を避けたい場合は実行せず、WebUI の status、Node、routing、Event の閲覧だけにしてください。

## 確認と停止

```powershell
.\local-review.cmd status
.\local-review.cmd logs
.\local-review.cmd down
```

`down` はプロセスとcontainerだけを停止し、LETHEのローカル正本を削除しません。再度 `up` するとcommissioningは同一内容を検証して再利用し、会話を復元します。ローカル検証専用のCompose projectを使うため、通常のNanihold開発containerには干渉しません。

## fail-fast

次は別経路へfallbackせず開始前または応答採用前に失敗します。

- Claude Codeの実versionとcandidateの`adapter_version`が違う
- requested modelとClaude Codeが報告したactual modelが違う
- PilotHostのcandidate key、Bearer token、接続先が違う
- local modeでeffortが`low`以外、Fable/Opus、書込可能、または`observe_only`以外
- LETHE、DataSpace、承認済みRouteSnapshot、必須secretが欠ける

PilotHostはWindows側に動的portで立ち、ランダムBearer tokenで保護されます。Docker Desktop上のNaniholdは`host.docker.internal`から認証付きで直接接続し、Nanihold KernelへClaude固有CLI flagを持ち込みません。WSL内の別Docker daemonや中継containerには依存しません。
