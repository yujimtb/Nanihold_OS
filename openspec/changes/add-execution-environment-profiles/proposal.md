# Change Proposal: add-execution-environment-profiles

**Version:** 1.0
**Date:** 2026-07-21
**Status:** Proposed
**Repository:** Nanihold_OS
**Type:** ExecutionEnvironment プロファイル機構の新設(candidate identity・PilotHost 実行境界に影響)
**Source:** オーナー合意設計(2026-07-21)。2026-07-21 のオーナー承認決定(環境の 2 層を「環境契約 / 環境実体」へ再設計、dispatch 時バージョン検証 + preflight キャッシュ)を反映して改訂。

---

## Why

エージェント実行環境の暗黙前提と、codex CLI のサンドボックス挙動の食い違いにより、WorkItem 実行が失敗した。

- codex CLI `0.144.5` は Windows ネイティブ環境で `--sandbox workspace-write` を **黙って read-only へ降格** させる。この降格により WorkItem 実行が書込拒否となり、`ProviderTimeout` で失敗した(2026-07-21、execution `e3123604` ほか)。
- 現在は暫定措置として、Windows 実行時のみ codex を `--dangerously-bypass-approvals-and-sandbox` で起動している。ただし宣言境界(Nanihold の sandbox-profile が定める書込ルート制限と `working_directory_allowlist`)は維持しており、境界そのものは緩めていない。
- `pilot-host.json` は実行環境が Windows ネイティブであることを暗黙の前提としており、executable の絶対パスやローカルパスの allowlist が直書きされている。したがって同一プロファイルを別環境(WSL / Docker)へ持ち運べない。
- オーナー方針: エージェントは Nanihold が動く環境を問わず実行可能であるべきである。開発は極力 WSL / Docker で行う(Mac 共同開発者とのパリティ確保)。
- 環境を具体名(`windows-native` / `wsl-ubuntu` 等)で列挙する設計は固定的で、自走する VSM は列挙の外に出た瞬間に停止する。オーナー不在時に生存できない。列挙ではなく「何が満たされていればよいか」の契約と、契約を満たす具体的実行場所(実体)の 2 層に分けるべきである(オーナー承認 2026-07-21)。
- CLI(codex-cli / claude-code)は自動更新され、そのタイミングは PilotHost の起動と一致しない。起動時 preflight だけでは、稼働中の更新を取りこぼす。

これらを解決するため、実行環境を「環境契約(何を満たすべきか / ポータブル)」と「環境実体(契約を満たす具体的実行場所 / S3 が自律管理)」の 2 層に分離し、PilotHost に環境吸収責務を持たせ、preflight を契約適合テストとして「サイレント降格」を検知し fail-fast する機構を仕様化する。あわせて preflight の起動を、起動時 1 回ではなく **タスク dispatch 直前の決定論的バージョン検証 + 検証結果キャッシュ** で駆動し、「変化を跨いだ最初の dispatch で 1 回」試走する方式を規定する。

## What Changes

- **ADDED:** 実行環境を **環境契約(EnvironmentContract / ポータブル)** と **環境実体(EnvironmentInstance / S3 が自律管理)** の 2 層に分離して定義する(EEP-01 / EEP-03)。環境契約は共通能力要件(シェル、workspace、メモリ、sandbox、パス論理名)と `adapters` 内のアダプタ別要件(endpoint・最低CLI版)の集合で宣言し、具体名で実行場所を列挙しない。環境実体は契約を満たす具体的実行場所(Windows ホスト・WSL・NAS コンテナ・将来のクラウド VM)で、S3 が自律的に発見・構築・検証・廃棄する。
- **ADDED:** candidate identity の `environment_fingerprint` を、**環境契約の正規化ハッシュ**として正式定義する(EEP-02)。環境実体側の機械固有情報(instance fingerprint を含む)はハッシュに含めない。
- **ADDED:** 環境契約アーティファクトのスケール時保管を規定する(EEP-04)。LETHE に版付きアーティファクトとして保存し、PilotHost が起動時にコントロールプレーンから取得する。
- **ADDED:** PilotHost の **環境吸収責務** を規定する(EEP-05)。実体種別に応じた argv 前置(`wsl -d <distro> --`、`docker compose exec` 等)、work_cwd / allowlist / 成果物パスの双方向変換、provider セッション記録(rollout)の読み出し先解決、エンドポイント URL(`localhost` vs `host.docker.internal`)の環境別書換え。
- **ADDED:** **Preflight = 契約適合テスト** を規定する(EEP-06)。PilotHost が dispatch 対象の adapter を 1 回試走させ、rollout の `sandbox_policy` と共通/対象アダプタ要件を実測し、契約に適合しない場合(サイレント降格・未宣言 adapter 等)は当該実体での実行を拒否して fail-fast する。合格実体は instance fingerprint 付きで Operational Ledger に記録する。
- **ADDED:** **dispatch 時バージョン検証 + preflight キャッシュ** を規定する(EEP-09)。PilotHost はタスク dispatch 直前に毎回、対象 adapter のCLI バージョンを決定論的に(バージョンファイル参照 + mtime 比較、プロセス起動不要)読む。検証タプル(adapter × CLI バージョン × sandbox モード × `environment_fingerprint`)が前回検証済みと一致すれば対象 adapter の preflight をスキップ、不一致なら対象要件でその場の preflight 試走 + 宣言メタデータの自動更新(FAV-06 連携)を行ってから実行する。検証結果キャッシュは Ledger または PilotHost ローカルに永続化し再起動を跨いで有効。これにより「起動時に 1 回」ではなく「変化を跨いだ最初の dispatch で 1 回」の試走になる。
- **ADDED:** **契約変更 = candidate 切替 / 実体フェイルオーバーは candidate 不変** を規定する(EEP-07)。契約が変われば `environment_fingerprint` が変わり別 candidate となり RouteSnapshot 承認制の枠内で切り替わる。同一契約を満たす実体間の切替は fingerprint を変えず candidate 切替にならない。
- **ADDED:** **オーナー承認境界** を規定する(EEP-10)。オーナー(S5)が承認するのは環境契約と調達ポリシー境界(使用可能な資源・ネットワーク・予算)のみ。個別実体の発見・構築・検証・廃棄は S3 の自律責務でオーナー逐次承認を要さない。
- **ADDED:** **環境実体の S3 自律ライフサイクル** を規定する(EEP-11)。S3 が契約適合の実体を発見・構築・検証・廃棄し、合格実体は instance fingerprint 付きで Operational Ledger に記録する。すべて調達ポリシー境界の内側で行う。
- **ADDED:** **契約適合フェイルオーバーと境界内自律再構築** を規定する(EEP-12)。実体が壊れたら契約適合の別実体を選び、無ければ境界内で新実体を自ら構築(例: Dockerfile から再構築)する。オーナー通知は非同期、事前承認不要。旧案の「承認済み環境セット内フェイルオーバー」は契約適合実体が複数ある場合の自然な帰結として吸収する。
- **ADDED:** 段階導入(Phase 0 / Phase 1 / Phase 2)を規定する(EEP-08)。Phase 0 は現行バイパス暫定措置(既実施)、Phase 1 で契約/実体機構と契約適合 preflight を導入し初期実体(Windows ネイティブ / WSL)を契約適合として登録して coding 既定を WSL へ切替えバイパスを撤去、Phase 2 で docker 実体と Mac 側実体を契約適合として追加する。

## Non-Goals

- codex CLI 自体の sandbox 実装の修正(サイレント降格そのものを CLI 側で直すことはしない)。
- Claude Code 固有の provider 機能追加やモデル選択ロジックの改修。
- 予算・タイムアウト政策(`token_budget` / 秒数制限)の変更。

## Affected Invariants

candidate identity の一意性は維持する。`environment_fingerprint` は環境契約のみから決まり、環境実体(機械固有パス・instance fingerprint 等)を変えても同一 candidate であり続ける。したがって同一契約を満たす実体間のフェイルオーバーは candidate を変えず RouteSnapshot 再発行を要さない。RouteSnapshot は承認制であり、同一 `route_key` で routable な snapshot は一つだけという不変条件を維持する。宣言境界(sandbox-profile の書込ルート制限と `working_directory_allowlist`)は本 change を通じて緩めない。preflight の失敗を宣言書き換えで回避しない(FAV-06)不変条件も維持する。

## Rollout

Phase 0 は既実施(Windows ネイティブ時のバイパス暫定措置)。Phase 1 の coding 既定切替(WSL へ)とバイパス撤去は、契約/実体機構・契約適合 preflight・dispatch 時バージョン検証 + キャッシュの実装完了とオーナー承認を経て実施する。オーナー承認は環境契約と調達ポリシー境界に限定し(EEP-10)、個別実体の発見・構築・検証・廃棄は S3 が境界内で自律的に行う。契約変更は RouteSnapshot 承認制の枠内で行い、旧版を明示 `RETIRED` にしてから後継を `PUBLISHED` にする。Phase 2(docker / Mac 実体)は Phase 1 安定後に着手する。
