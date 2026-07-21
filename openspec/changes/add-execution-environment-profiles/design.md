# Design: add-execution-environment-profiles

## Context

`pilot-host.json` は実行環境が Windows ネイティブであることを暗黙前提とし、executable 絶対パスとローカルパス allowlist を直書きしている。この前提のため、同一プロファイルを WSL / Docker へ持ち運べず、また codex CLI がプラットフォーム依存でサンドボックスを黙って降格させる挙動を検知できなかった。本 change は実行環境を、能力要件の宣言である **環境契約(EnvironmentContract)** と、契約を満たす具体的実行場所である **環境実体(EnvironmentInstance)** の 2 層に分離し、環境差の吸収を PilotHost に集約し、降格を契約適合 preflight で fail-fast させる。

## 環境の 2 層再設計(オーナー承認 2026-07-21)

環境を具体名(`windows-native` / `wsl-ubuntu` 等)で列挙する設計は固定的で、自走する VSM は列挙の外に出た瞬間に停止し、オーナー不在時に生存できない。そこで環境を次の 2 層で定義する。

- **環境契約(EnvironmentContract)**: 「何が満たされていればよいか」の宣言。例: POSIX シェル、`api.openai.com` 疎通、workspace 書き込み可、最低メモリ、要求 sandbox モード、パス論理名。ポータブルで機械非依存。オーナー(S5)が承認するのはこの契約と調達ポリシーの境界(使用可能な資源・ネットワーク・予算)のみ。
- **環境実体(EnvironmentInstance)**: 契約を満たす具体的実行場所(Windows ホスト・WSL・NAS コンテナ・将来のクラウド VM)。S3 が自律的に発見・構築・検証・廃棄する。preflight は契約適合テストとなり、合格した実体は instance fingerprint 付きで Operational Ledger に記録される。
- **フェイルオーバー**: 実体が壊れたら契約適合の別実体を選ぶ。無ければポリシー境界内で新実体を自ら構築する(例: Dockerfile から再構築)。オーナー通知は非同期、承認は不要(境界の承認は済んでいるため)。旧案の「承認済み環境セット内フェイルオーバー」は、契約適合実体が複数ある場合の自然な帰結として本設計に吸収される。

## 診断で判明した事実(背景)

- codex CLI `0.144.5` は Windows ネイティブで `--sandbox workspace-write` を黙って read-only に降格させる。宣言では workspace-write を要求していても、実際の rollout の `sandbox_policy` は read-only になる。
- この降格により WorkItem 実行が書込拒否になり `ProviderTimeout` で失敗した(execution `e3123604` ほか、2026-07-21)。
- 暫定措置として Windows 実行時のみ `--dangerously-bypass-approvals-and-sandbox` で起動している。Nanihold 側の宣言境界(sandbox-profile の書込ルート制限・`working_directory_allowlist`)は維持しており、緩めていない。

## 2 層分離の設計

### 環境契約(ポータブル)

環境が満たすべき能力を機械非依存で表す。以下を含む。

- 要求能力: POSIX シェル、`api.openai.com` 疎通、workspace 書き込み可、最低メモリ等。
- `supported_sandboxes` / 要求 sandbox モード: この契約が要求し、実体が実際に提供できるべき sandbox モード(例: `["workspace-write", "read-only"]`)。
- 最低要求 CLI バージョン(任意): 版制約が必要な場合のみ宣言する下限。identity には含めない(FAV 方針に整合)。
- パス写像の論理名: 例 `workspace-root`。物理パスではなく論理名のみを宣言する。

環境契約は具体的な実行場所の名前(`windows-native` 等)を identity 要素として列挙しない。`environment_fingerprint` はこの環境契約の正規化ハッシュとして定義する。機械固有情報・実体識別情報は一切含めない。スケール時は環境契約を LETHE に版付きアーティファクトとして保存し、PilotHost が起動時にコントロールプレーンから取得する。

### 環境実体(具体的実行場所)

環境契約を満たす具体的実行場所。契約の論理要件をその場所の実体へ束縛する。以下を含む。

- 論理名→機械固有パス(例: `workspace-root = D:\userdata\docs\projects`)。
- CLI 実体パス。
- `CODEX_HOME` 等の環境変数。

環境実体の識別情報(instance fingerprint を含む機械固有情報)は `environment_fingerprint` に **含めない**。開発時はローカルファイル、スケール時は provisioning / 環境変数で注入する。同一契約に対して実体が違っても candidate としては同一である。実体は S3 が自律的に発見・構築・検証・廃棄し、契約適合テストに合格した実体は instance fingerprint 付きで Operational Ledger に記録される。

## PilotHost の環境吸収責務

PilotHost は選択した環境実体の種別と実体情報を突き合わせ、以下を吸収する。

- argv 前置: 実体種別に応じて `wsl -d <distro> --` や `docker compose exec <svc>` 等を実行 argv の先頭へ付す。
- パス双方向変換: work_cwd / allowlist / 成果物パスを、ホスト表現とゲスト(WSL / コンテナ)表現の間で往復変換する。
- rollout 読み出し先解決: provider セッション記録(rollout)の実ファイル位置を環境別に解決する。
- エンドポイント URL 書換え: `localhost` と `host.docker.internal` 等を環境別に書き換える。

## Preflight = 契約適合テスト(中核)

preflight は環境実体が環境契約に適合するかを実測で確かめる契約適合テストである。codex を 1 回試走させ、生成された rollout の `sandbox_policy` を読み、契約の `supported_sandboxes` / 要求モードおよび他の能力要件(疎通・書き込み可・最低メモリ等)と突き合わせる。

- 適合すれば実行を継続し、合格実体を instance fingerprint 付きで Operational Ledger に記録する。
- 不適合(要求 workspace-write に対し実測 read-only 等のサイレント降格)なら **当該実体での実行を拒否して fail-fast** する。

オーナー評: 「コンテクストを先に注入するよりも合理的」。実行前に環境の実挙動を実測し、契約と食い違う実体では走らせない。

## dispatch 時バージョン検証 + preflight キャッシュ

CLI(codex-cli / claude-code)は自動更新され、そのタイミングは PilotHost の起動と一致しない。起動時 preflight だけでは稼働中の更新を取りこぼす。そこで preflight の起動を dispatch 駆動 + キャッシュにする。

- PilotHost はタスク dispatch 直前に毎回、CLI の実バージョンを **決定論的に** 読む(バイナリ/パッケージのバージョンファイル参照 + mtime 比較。プロセス起動不要でコストほぼゼロ)。
- 検証タプル(CLI バージョン × sandbox モード × `environment_fingerprint`)が前回検証済みタプルと一致すれば preflight をスキップ(キャッシュヒット)。
- 不一致(キャッシュミス)なら、その場で preflight 試走(契約適合テスト)+ 宣言メタデータの自動更新(RouteSnapshot 候補の最低要求版メモ等、FAV-06 連携)を行ってから本実行へ進む。
- 検証結果キャッシュは Ledger または PilotHost ローカルに永続化し、再起動を跨いで有効。
- 帰結: 「起動時に 1 回」ではなく「変化(CLI 版 / sandbox モード / 契約)を跨いだ最初の dispatch で 1 回」の試走になる。プロセス起動を伴う試走はキャッシュミス時に限られる。
- preflight が失敗した場合は、降格・非互換を「宣言を書き換えて通す」ことはせず fail-fast する(FAV-06 の禁止事項と整合)。

## 環境切替 = candidate 切替 / 実体フェイルオーバーは candidate 不変

環境契約が変われば `environment_fingerprint` が変わり、Bayesian routing 上は別 candidate となる。したがって契約変更は RouteSnapshot の承認制(`register → S3_STAR_APPROVED → OWNER_APPROVED → PUBLISHED`、旧版は `superseded_by_approved_snapshot` 理由の human Event で `RETIRED`)の枠内で監査可能に行う。同一契約を満たす実体間のフェイルオーバー(壊れた実体から別実体への切替、または境界内での新実体構築)は fingerprint を変えず candidate 切替にならないため、RouteSnapshot 再発行を要さない。

## オーナー承認境界と S3 自律ライフサイクル

オーナー(S5)が承認するのは環境契約と調達ポリシー境界(使用可能な資源・ネットワーク・予算)のみ。個別実体の発見・構築・検証・廃棄は S3 がその境界内で自律的に行い、逐次のオーナー承認を要さない。実体が壊れたときのフェイルオーバー・境界内再構築も同様に事前承認不要で、オーナー通知は非同期でよい。これにより VSM は列挙済み環境の外へ出ても、契約適合という判定基準のもとで自走・生存できる。

## 段階導入

- **Phase 0(既実施)**: Windows ネイティブ時のバイパス暫定措置。宣言境界は維持。
- **Phase 1**: 契約/実体機構 + 契約適合 preflight + dispatch 時バージョン検証/キャッシュを導入。初期実体(Windows ネイティブ / WSL)を契約適合として登録し、coding 既定を WSL へ切替え、バイパスを撤去する。これらの実体は具体名で固定されるのではなく、契約に適合する実体として登録される。
- **Phase 2**: docker 実体と Mac 側実体を、同一契約に適合する新実体として追加。

## 決定と根拠

- **契約 / 実体の 2 層**: 環境を具体名で列挙すると固定的で、列挙の外に出た瞬間に VSM が停止する。契約(何を満たすべきか)と実体(それを満たす具体的実行場所)に分け、オーナーは契約と境界のみ承認し、実体は S3 が自律管理することで、列挙の外でも契約適合という判定で自走できる。
- **fingerprint は環境契約のみ**: 実体側の機械固有情報を含めると同一意味の環境が実体ごとに別 candidate になり、routing の学習が分散する。契約のみのハッシュにすることで、意味が同じ環境は同一 candidate に集約され、契約が変わったときだけ candidate が切り替わる。実体フェイルオーバーは candidate を変えない。
- **preflight で fail-fast**: サイレント降格は実行時まで気付けず ProviderTimeout として現れる。契約適合テストとして 1 回試走して実測することで、失敗を実行前に確定的に検知する。
- **dispatch 駆動 + キャッシュ**: CLI 自動更新は起動タイミングと一致しないため、起動時 1 回では取りこぼす。dispatch 直前の決定論的バージョン読み(mtime 比較、プロセス起動不要)でタプル一致を判定し、変化時のみ試走することで、取りこぼしを防ぎつつ試走コストを最小化する。
- **環境吸収は PilotHost に集約**: argv 前置・パス変換・URL 書換えを一箇所に集約し、環境契約をポータブルに保つ。

## リスクと対応

- **preflight の試走コスト**: キャッシュミス時に codex を 1 回余分に走らせる。dispatch 直前のバージョン読みは mtime 比較でプロセス起動を伴わず、試走はキャッシュミス時のみに限る。試走は本実行の budget とは別枠にする(policy 変更は本 change のスコープ外)。
- **契約スキーマの粒度**: `supported_sandboxes` や能力要件・最低要求 CLI バージョンの表現粒度が粗いと降格検知の精度が落ちる。スキーマ粒度はオーナーレビュー論点とする。
- **実体の自律構築の安全性**: S3 が境界内で新実体を構築(例: Dockerfile 再構築)する際、調達ポリシー境界(資源・ネットワーク・予算)を越えないことが安全性の要である。境界の表現と執行方法はオーナーレビュー論点とする。
- **キャッシュの陳腐化**: バージョンファイルの mtime が更新されない形の CLI 更新があると検知漏れの恐れがある。検証タプルに `environment_fingerprint` と sandbox モードも含めることで、契約・モード変更は別経路でも検知される。mtime 以外の検知強化は follow-up とする。
- **Phase 1 の切替タイミング**: coding 既定を WSL へ移す時点でバイパスを撤去するため、preflight が WSL 実体で確実に通ることを先に検証する必要がある。切替タイミングはオーナー承認事項とする。
