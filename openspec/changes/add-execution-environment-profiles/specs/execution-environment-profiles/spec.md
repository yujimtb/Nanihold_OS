## ADDED Requirements

### Requirement: EEP-01 宣言層(ポータブル)の定義
システムは `ExecutionEnvironment` プロファイルの宣言層を、機械非依存のポータブルな層として定義しなければならない(SHALL)。宣言層は `kind`(`native` / `wsl` / `docker`)・`supported_sandboxes`・要求 CLI バージョン・パス写像の論理名(例: `workspace-root`)を含まなければならない(SHALL)。宣言層に機械固有パスや CLI 実体パス等の機械固有情報を含めてはならない(SHALL NOT)。

#### Scenario: 宣言層が機械非依存の項目のみを持つ
- **GIVEN** `ExecutionEnvironment` プロファイルの宣言層
- **WHEN** その内容を確認する
- **THEN** `kind`・`supported_sandboxes`・要求 CLI バージョン・パスの論理名だけが含まれ、機械固有パスや実体パスは含まれない

#### Scenario: 同一宣言層は別機械へ持ち運べる
- **GIVEN** ある機械で定義された宣言層
- **WHEN** それを別環境(WSL / Docker / 別ホスト)へ持ち込む
- **THEN** 宣言層は書き換え不要でそのまま流用でき、機械差は束縛層側で吸収される

### Requirement: EEP-02 environment_fingerprint の正式定義
システムは candidate identity の `environment_fingerprint` を、宣言層の正規化ハッシュとして定義しなければならない(SHALL)。`environment_fingerprint` の計算に束縛層(機械固有パス・CLI 実体パス・`CODEX_HOME` 等)を含めてはならない(SHALL NOT)。

#### Scenario: fingerprint は宣言層のみから決まる
- **GIVEN** ある宣言層
- **WHEN** `environment_fingerprint` を計算する
- **THEN** 宣言層の正規化ハッシュのみが結果を決め、束縛層の値は結果に影響しない

#### Scenario: 束縛層だけの変更では fingerprint が変わらない
- **GIVEN** 同一の宣言層に対して束縛層(例: `workspace-root` の物理パス)だけを変更する
- **WHEN** `environment_fingerprint` を再計算する
- **THEN** fingerprint は変わらず、同一 candidate であり続ける

### Requirement: EEP-03 束縛層(ローカル)の定義
システムは束縛層を、宣言層の論理名をその機械の実体へ束縛する層として定義しなければならない(SHALL)。束縛層は論理名→機械固有パス(例: `workspace-root = D:\userdata\docs\projects`)・CLI 実体パス・`CODEX_HOME` 等を含み、開発時はローカルファイル、スケール時は provisioning / 環境変数で注入されなければならない(SHALL)。束縛層を identity ハッシュに含めてはならない(SHALL NOT)。

#### Scenario: 束縛層が論理名を機械実体へ束縛する
- **GIVEN** 宣言層の論理名 `workspace-root`
- **WHEN** ある機械で束縛層を適用する
- **THEN** `workspace-root` はその機械固有パスへ束縛され、CLI 実体パス・`CODEX_HOME` も束縛される

#### Scenario: 束縛層はローカル/注入で供給される
- **GIVEN** 開発環境とスケール環境
- **WHEN** 束縛層を供給する
- **THEN** 開発時はローカルファイルから、スケール時は provisioning / 環境変数から注入され、いずれも identity ハッシュには含まれない

### Requirement: EEP-04 宣言層アーティファクトのスケール時保管
システムはスケール時に宣言層を LETHE の版付きアーティファクトとして保存しなければならない(SHALL)。PilotHost は起動時にコントロールプレーンから当該宣言層アーティファクトを取得しなければならない(SHALL)。

#### Scenario: PilotHost が起動時に宣言層を取得する
- **GIVEN** LETHE に版付きで保存された宣言層アーティファクト
- **WHEN** PilotHost がスケール環境で起動する
- **THEN** コントロールプレーン経由で当該版の宣言層を取得してから実行準備を進める

### Requirement: EEP-05 PilotHost の環境吸収責務
システムは PilotHost に、宣言層の `kind` と束縛層の実体に応じた環境差の吸収責務を負わせなければならない(SHALL)。PilotHost は `kind` に応じた argv 前置(`wsl -d <distro> --`・`docker compose exec` 等)、work_cwd / allowlist / 成果物パスの双方向変換、provider セッション記録(rollout)の読み出し先解決、エンドポイント URL(`localhost` vs `host.docker.internal`)の環境別書換えを行わなければならない(SHALL)。

#### Scenario: kind に応じて argv を前置する
- **GIVEN** `kind = wsl` の宣言層と対応する束縛層
- **WHEN** PilotHost が codex を起動する
- **THEN** 実行 argv の先頭に `wsl -d <distro> --` が前置される

#### Scenario: パスを双方向変換する
- **GIVEN** ホスト表現の work_cwd / allowlist / 成果物パス
- **WHEN** PilotHost がゲスト(WSL / コンテナ)へ渡し、結果を受け取る
- **THEN** パスはホスト表現とゲスト表現の間で往復変換される

#### Scenario: エンドポイント URL を環境別に書き換える
- **GIVEN** `kind = docker` の環境
- **WHEN** PilotHost がエンドポイント URL を解決する
- **THEN** `localhost` が `host.docker.internal` 等の環境別表現へ書き換えられる

### Requirement: EEP-06 起動時 Preflight 検証による fail-fast
システムは PilotHost 起動時に codex を 1 回試走させ、生成された rollout の `sandbox_policy` を宣言層の `supported_sandboxes` および要求モードと突き合わせなければならない(SHALL)。両者が一致しない場合(サイレント降格)、システムは起動を拒否して fail-fast しなければならない(SHALL)。不一致を検知したまま本実行へ進んではならない(SHALL NOT)。

#### Scenario: 一致すれば起動を継続する
- **GIVEN** 宣言が workspace-write を要求し、preflight 試走の rollout も `sandbox_policy = workspace-write` である
- **WHEN** PilotHost が preflight を評価する
- **THEN** 宣言と実測が一致するため起動を継続する

#### Scenario: サイレント降格で起動を拒否する
- **GIVEN** 宣言が workspace-write を要求するが、preflight 試走の rollout の `sandbox_policy` が read-only へ降格している
- **WHEN** PilotHost が preflight を評価する
- **THEN** 不一致として起動を拒否し fail-fast し、本実行を開始しない

### Requirement: EEP-07 環境切替 = candidate 切替の承認制
システムは宣言層の変更を candidate の切替として扱わなければならない(SHALL)。宣言層が変われば `environment_fingerprint` が変わり別 candidate となり、切替は RouteSnapshot の承認制(`register → S3_STAR_APPROVED → OWNER_APPROVED → PUBLISHED`、旧版は `superseded_by_approved_snapshot` 理由の human Event で `RETIRED`)の枠内で行わなければならない(SHALL)。旧版 retirement と後継 publish を一操作へまとめてはならない(SHALL NOT)。

#### Scenario: 宣言層変更が別 candidate になる
- **GIVEN** `kind` を `native` から `wsl` へ変える宣言層変更
- **WHEN** `environment_fingerprint` を再計算する
- **THEN** fingerprint が変わり、別 candidate として扱われる

#### Scenario: 承認制の枠内で切替える
- **GIVEN** 新環境の宣言層に基づく後継 RouteSnapshot 候補
- **WHEN** register から S3* 承認・owner 承認を経て publish する
- **THEN** 旧 PUBLISHED snapshot が先に RETIRED になってから後継が PUBLISHED になる

### Requirement: EEP-08 段階導入
システムは本機構を段階導入しなければならない(SHALL)。Phase 0 は現行バイパス暫定措置(既実施)、Phase 1 はプロファイル機構と `env:windows-native` / `env:wsl-ubuntu` と preflight を導入し coding 既定を WSL へ切替えてバイパスを撤去、Phase 2 は `kind: docker` と Mac 側プロファイルを追加するものでなければならない(SHALL)。Phase 1 でバイパスを撤去する前に、preflight が対象環境で通ることを検証しなければならない(SHALL)。

#### Scenario: Phase 1 でバイパスを撤去する
- **GIVEN** Phase 1 でプロファイル機構と preflight が導入され、coding 既定を WSL へ切替える
- **WHEN** WSL 環境で preflight が宣言どおり通ることを検証する
- **THEN** Windows ネイティブ時のバイパス暫定措置(`--dangerously-bypass-approvals-and-sandbox`)を撤去する

#### Scenario: Phase 2 で Docker/Mac を追加する
- **GIVEN** Phase 1 が安定している
- **WHEN** Phase 2 に着手する
- **THEN** `kind: docker` と Mac 側プロファイルを追加する
