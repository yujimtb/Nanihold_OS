## ADDED Requirements

### Requirement: EEP-01 環境契約(EnvironmentContract)の定義
システムは実行環境を、まず「何が満たされていればよいか」を宣言する **環境契約(EnvironmentContract)** として定義しなければならない(SHALL)。環境契約は満たすべき能力要件を宣言しなければならず(SHALL)、少なくとも次を含む: `supported_shells` による1つ以上のシェル種別の集合(いずれかに適合するOR要件。例: POSIX / PowerShell シェル)、要求エンドポイント疎通(例: `api.openai.com` 到達可能)、workspace 書き込み可能、最低メモリ、要求 sandbox モード(`supported_sandboxes`)、パス写像の論理名(例: `workspace-root`)、および版制約が必要な場合の最低要求 CLI バージョン(任意)。環境契約は機械非依存でポータブルでなければならない(SHALL)。環境契約は具体的な実行場所の名前(例: `windows-native` / `wsl-ubuntu`)を identity の要素として列挙してはならない(SHALL NOT)。環境契約に機械固有パス・CLI 実体パス等の機械固有情報を含めてはならない(SHALL NOT)。

#### Scenario: 契約が能力要件で表される
- **GIVEN** ある WorkItem の実行に必要な環境
- **WHEN** その環境契約を確認する
- **THEN** 1つ以上のシェル種別の集合(OR適合)・エンドポイント疎通・workspace 書き込み可・最低メモリ・要求 sandbox モード・パス論理名といった能力要件で表され、具体的実行場所の名前や機械固有パスは含まれない

#### Scenario: 同一契約は別実体へ持ち運べる
- **GIVEN** ある機械で定義された環境契約
- **WHEN** それを別の実行場所(WSL / NAS コンテナ / 別ホスト / 将来のクラウド VM)へ持ち込む
- **THEN** 契約は書き換え不要でそのまま流用でき、実行場所の差は環境実体側で吸収される

### Requirement: EEP-02 environment_fingerprint の正式定義
システムは candidate identity の `environment_fingerprint` を、環境契約の正規化ハッシュとして定義しなければならない(SHALL)。`environment_fingerprint` の計算に環境実体側の機械固有情報(機械固有パス・CLI 実体パス・`CODEX_HOME` 等、および実体を識別する instance fingerprint)を含めてはならない(SHALL NOT)。

#### Scenario: fingerprint は環境契約のみから決まる
- **GIVEN** ある環境契約
- **WHEN** `environment_fingerprint` を計算する
- **THEN** 環境契約の正規化ハッシュのみが結果を決め、環境実体側の値は結果に影響しない

#### Scenario: 実体だけの変更では fingerprint が変わらない
- **GIVEN** 同一の環境契約に対して環境実体(例: `workspace-root` の物理パスや実行ホスト)だけを変更する
- **WHEN** `environment_fingerprint` を再計算する
- **THEN** fingerprint は変わらず、同一 candidate であり続ける

### Requirement: EEP-03 環境実体(EnvironmentInstance)の定義
システムは環境契約を満たす具体的な実行場所を **環境実体(EnvironmentInstance)** として定義しなければならない(SHALL)。環境実体は Windows ホスト・WSL・NAS コンテナ・将来のクラウド VM 等の具体的実行場所であり、契約の論理要件をその場所の実体へ束縛(論理名→機械固有パス、CLI 実体パス、`CODEX_HOME` 等)しなければならない(SHALL)。環境実体は S3 が自律的に発見・構築・検証・廃棄しなければならず(SHALL)、その識別情報(instance fingerprint 等の機械固有情報)を candidate identity ハッシュに含めてはならない(SHALL NOT)。

#### Scenario: 実体が契約の論理要件を機械実体へ束縛する
- **GIVEN** 環境契約の論理名 `workspace-root` と最低メモリ要件
- **WHEN** ある具体的実行場所を環境実体として適用する
- **THEN** `workspace-root` はその場所の機械固有パスへ束縛され、CLI 実体パス・`CODEX_HOME` も束縛される

#### Scenario: 実体は identity に影響しない
- **GIVEN** 同一契約を満たす複数の環境実体(例: Windows ホストと WSL)
- **WHEN** それぞれで candidate identity を計算する
- **THEN** どの実体で走っても `environment_fingerprint` は同一で、実体差は candidate identity に現れない

### Requirement: EEP-04 環境契約アーティファクトのスケール時保管
システムはスケール時に環境契約を LETHE の版付きアーティファクトとして保存しなければならない(SHALL)。PilotHost は起動時にコントロールプレーンから当該環境契約アーティファクトを取得しなければならない(SHALL)。

#### Scenario: PilotHost が起動時に環境契約を取得する
- **GIVEN** LETHE に版付きで保存された環境契約アーティファクト
- **WHEN** PilotHost がスケール環境で起動する
- **THEN** コントロールプレーン経由で当該版の環境契約を取得してから実行準備を進める

### Requirement: EEP-05 PilotHost の環境吸収責務
システムは PilotHost に、選択した環境実体に応じた環境差の吸収責務を負わせなければならない(SHALL)。PilotHost は実体の種別(native / wsl / docker 等)に応じた argv 前置(`wsl -d <distro> --`・`docker compose exec` 等)、work_cwd / allowlist / 成果物パスの双方向変換、provider セッション記録(rollout)の読み出し先解決、エンドポイント URL(`localhost` vs `host.docker.internal`)の環境別書換えを行わなければならない(SHALL)。

#### Scenario: 実体種別に応じて argv を前置する
- **GIVEN** WSL 上の環境実体
- **WHEN** PilotHost が codex を起動する
- **THEN** 実行 argv の先頭に `wsl -d <distro> --` が前置される

#### Scenario: パスを双方向変換する
- **GIVEN** ホスト表現の work_cwd / allowlist / 成果物パス
- **WHEN** PilotHost がゲスト(WSL / コンテナ)へ渡し、結果を受け取る
- **THEN** パスはホスト表現とゲスト表現の間で往復変換される

#### Scenario: エンドポイント URL を環境別に書き換える
- **GIVEN** コンテナ上の環境実体
- **WHEN** PilotHost がエンドポイント URL を解決する
- **THEN** `localhost` が `host.docker.internal` 等の環境別表現へ書き換えられる

### Requirement: EEP-06 Preflight = 契約適合テストによる fail-fast
システムは preflight を、環境実体が環境契約に適合するかを実測で確かめる **契約適合テスト** として実行しなければならない(SHALL)。preflight は codex を 1 回試走させ、生成された rollout の `sandbox_policy` を環境契約の `supported_sandboxes` および要求モードと突き合わせ、あわせて契約が要求する他の能力(エンドポイント疎通・workspace 書き込み可・最低メモリ等)を実測しなければならない(SHALL)。契約に適合しない場合(例: 要求 workspace-write に対する rollout のサイレント read-only 降格)、システムは当該実体での実行を拒否して fail-fast しなければならない(SHALL)。不適合を検知したまま本実行へ進んではならない(SHALL NOT)。契約に合格した実体は instance fingerprint 付きで Operational Ledger に記録されなければならない(SHALL)。

#### Scenario: 契約適合なら実行を継続する
- **GIVEN** 契約が workspace-write を要求し、preflight 試走の rollout も `sandbox_policy = workspace-write` で他の能力要件も満たす
- **WHEN** PilotHost が契約適合テストを評価する
- **THEN** 契約と実測が一致するため実行を継続し、合格実体を instance fingerprint 付きで Operational Ledger に記録する

#### Scenario: サイレント降格で当該実体を拒否する
- **GIVEN** 契約が workspace-write を要求するが、preflight 試走の rollout の `sandbox_policy` が read-only へ降格している
- **WHEN** PilotHost が契約適合テストを評価する
- **THEN** 不適合として当該実体での実行を拒否し fail-fast し、本実行を開始しない

### Requirement: EEP-07 契約変更 = candidate 切替 / 実体フェイルオーバーは candidate 不変
システムは環境契約の変更を candidate の切替として扱わなければならない(SHALL)。契約が変われば `environment_fingerprint` が変わり別 candidate となり、切替は RouteSnapshot の承認制(`register → S3_STAR_APPROVED → OWNER_APPROVED → PUBLISHED`、旧版は `superseded_by_approved_snapshot` 理由の human Event で `RETIRED`)の枠内で行わなければならない(SHALL)。旧版 retirement と後継 publish を一操作へまとめてはならない(SHALL NOT)。同一契約を満たす環境実体の切替(フェイルオーバー)は `environment_fingerprint` を変えず、candidate 切替として扱ってはならない(SHALL NOT)。

#### Scenario: 契約変更が別 candidate になる
- **GIVEN** 要求能力(例: 最低メモリや要求 sandbox モード)を変える環境契約の変更
- **WHEN** `environment_fingerprint` を再計算する
- **THEN** fingerprint が変わり、別 candidate として RouteSnapshot 承認制の枠内で切り替わる

#### Scenario: 実体フェイルオーバーは candidate を変えない
- **GIVEN** 同一契約を満たす実体 A が壊れ、同じ契約を満たす実体 B へ切り替える
- **WHEN** `environment_fingerprint` を再計算する
- **THEN** fingerprint は変わらず同一 candidate のままで、RouteSnapshot 再発行は不要である

### Requirement: EEP-08 段階導入
システムは本機構を段階導入しなければならない(SHALL)。Phase 0 は現行バイパス暫定措置(既実施)、Phase 1 は環境契約/環境実体の機構と契約適合 preflight を導入し初期実体(Windows ネイティブ / WSL)を契約適合として登録して coding 既定を WSL へ切替えてバイパスを撤去、Phase 2 は docker 実体と Mac 側実体を契約適合として追加するものでなければならない(SHALL)。Phase 1 でバイパスを撤去する前に、契約適合 preflight が対象実体で通ることを検証しなければならない(SHALL)。段階導入の各実体は具体名で固定されるのではなく、契約に適合する実体として登録される(SHALL)。

#### Scenario: Phase 1 でバイパスを撤去する
- **GIVEN** Phase 1 で契約/実体機構と契約適合 preflight が導入され、coding 既定を WSL 実体へ切替える
- **WHEN** WSL 実体で preflight が契約どおり通ることを検証する
- **THEN** Windows ネイティブ時のバイパス暫定措置(`--dangerously-bypass-approvals-and-sandbox`)を撤去する

#### Scenario: Phase 2 で Docker/Mac 実体を追加する
- **GIVEN** Phase 1 が安定している
- **WHEN** Phase 2 に着手する
- **THEN** docker 実体と Mac 側実体を、同一契約に適合する新実体として追加する

### Requirement: EEP-09 dispatch 時バージョン検証と preflight キャッシュ
システムは PilotHost に、タスク dispatch 直前に毎回、CLI(codex-cli / claude-code)の実バージョンを **決定論的に**(バイナリ/パッケージのバージョンファイル参照 + mtime 比較で、プロセス起動を伴わずコストほぼゼロで)読み取らせなければならない(SHALL)。PilotHost は検証タプル(CLI バージョン × sandbox モード × `environment_fingerprint`)を保持し、dispatch 時のタプルが前回検証済みタプルと一致する場合は preflight をスキップ(キャッシュヒット)しなければならない(SHALL)。タプルが不一致(キャッシュミス)の場合、システムはその場で preflight 試走(EEP-06 の契約適合テスト)を行い、あわせて追従して安全な宣言メタデータ(RouteSnapshot 候補の最低要求版メモ等)の自動更新(FAV-06)を行ってから本実行へ進まなければならない(SHALL)。検証結果キャッシュは Ledger または PilotHost ローカルに永続化され、再起動を跨いで有効でなければならない(SHALL)。preflight 試走はキャッシュミス時に限られ、変化が無い間の dispatch でプロセス起動を伴う試走を繰り返してはならない(SHALL NOT)。キャッシュミス時の preflight が失敗した場合、宣言を書き換えて通すことはせず(FAV-06 参照)fail-fast しなければならない(SHALL)。

#### Scenario: 変化が無ければ preflight をスキップする
- **GIVEN** 前回検証済みタプル(CLI バージョン × sandbox モード × `environment_fingerprint`)と一致する状態で新しいタスクを dispatch する
- **WHEN** PilotHost が dispatch 直前にバージョンファイルを mtime 比較で決定論的に読み、タプルを突き合わせる
- **THEN** キャッシュヒットとして preflight 試走をスキップし、プロセス起動を伴う試走を行わずに本実行へ進む

#### Scenario: 稼働中の CLI 自動更新を跨いだ最初の dispatch で試走する
- **GIVEN** PilotHost 稼働中に CLI がバージョンファイルの mtime 変化を伴って自動更新された
- **WHEN** 更新後に最初のタスクを dispatch する
- **THEN** キャッシュミスとして、その場で preflight 試走(契約適合テスト)と宣言メタデータの自動更新(FAV-06)を行ってから本実行へ進み、検証結果を新タプルでキャッシュに永続化する

#### Scenario: キャッシュは再起動を跨いで有効
- **GIVEN** 検証済みタプルが Ledger または PilotHost ローカルに永続化されている
- **WHEN** PilotHost を再起動し、変化の無い状態で最初の dispatch を行う
- **THEN** 永続化キャッシュがヒットし、再起動を理由とした余分な preflight 試走は行われない

#### Scenario: キャッシュミス時の preflight 失敗は宣言書き換えで回避しない
- **GIVEN** キャッシュミスで走らせた preflight(契約適合テスト)が sandbox 降格・非互換で失敗する
- **WHEN** システムがその失敗を処理する
- **THEN** 宣言を書き換えて検証を通すことはせず、自動更新を行わずに fail-fast する

### Requirement: EEP-10 オーナー承認は環境契約と調達ポリシー境界に限定
システムはオーナー(S5)の承認対象を、環境契約と調達ポリシーの境界(使用可能な資源・ネットワーク・予算)に限定しなければならない(SHALL)。オーナーは個別の環境実体の発見・構築・検証・廃棄を逐一承認してはならず(SHALL NOT)、それらは S3 の自律責務とする。ポリシー境界内で行われる実体操作にオーナーの事前承認を要求してはならない(SHALL NOT)。

#### Scenario: オーナーは契約と境界のみを承認する
- **GIVEN** ある環境契約と、使用可能な資源・ネットワーク・予算を定める調達ポリシー境界
- **WHEN** オーナー(S5)が承認を行う
- **THEN** 承認対象は環境契約と調達ポリシー境界のみであり、個別実体の構築・廃棄はオーナー承認の対象に含まれない

#### Scenario: 境界内の実体操作は事前承認不要
- **GIVEN** 調達ポリシー境界内での環境実体の構築・検証・廃棄
- **WHEN** S3 がそれらを実行する
- **THEN** オーナーの事前承認を要さず自律的に実行される

### Requirement: EEP-11 環境実体の S3 自律ライフサイクルと Operational Ledger 記録
システムは S3 に、環境契約に適合する環境実体を自律的に発見・構築・検証・廃棄させなければならない(SHALL)。検証は EEP-06 の契約適合テスト(preflight)として行わなければならず(SHALL)、合格した実体は instance fingerprint 付きで Operational Ledger に記録されなければならない(SHALL)。廃棄された実体もその履歴が Operational Ledger から追跡可能でなければならない(SHALL)。実体の発見・構築・検証・廃棄はいずれも調達ポリシー境界(EEP-10)の内側で行わなければならない(SHALL)。

#### Scenario: 合格実体を fingerprint 付きで記録する
- **GIVEN** S3 が契約適合の環境実体を発見または構築し、契約適合テストに合格させる
- **WHEN** その実体を稼働可能として登録する
- **THEN** 実体は instance fingerprint 付きで Operational Ledger に記録され、後から監査・選択できる

#### Scenario: ライフサイクルは境界内で自律実行される
- **GIVEN** 調達ポリシー境界内の資源・ネットワーク・予算
- **WHEN** S3 が実体を発見・構築・検証・廃棄する
- **THEN** これらは境界の内側で自律的に行われ、各操作が Operational Ledger に記録される

### Requirement: EEP-12 契約適合フェイルオーバーと境界内自律再構築
システムは稼働中の環境実体が壊れた場合、同一環境契約に適合する別実体を選んでフェイルオーバーしなければならない(SHALL)。契約適合の別実体が存在しない場合、システムは調達ポリシー境界(EEP-10)の内側で新しい実体を自ら構築(例: Dockerfile から再構築)してから契約適合テストで検証しなければならない(SHALL)。フェイルオーバーおよび境界内の新実体構築にオーナーの事前承認を要求してはならず(SHALL NOT)、オーナーへの通知は非同期でよい(MAY)。旧案の「承認済み環境セット内フェイルオーバー」は、契約適合実体が複数存在する場合の自然な帰結として本設計に吸収される(SHALL)。

#### Scenario: 別の契約適合実体へフェイルオーバーする
- **GIVEN** 稼働中の実体 A が壊れ、同一契約に適合する実体 B が Operational Ledger に登録されている
- **WHEN** システムがフェイルオーバーを行う
- **THEN** 実体 B へ切り替えて実行を継続し、オーナー承認を要さず、通知は非同期に行う(`environment_fingerprint` は不変で candidate は変わらない)

#### Scenario: 適合実体が無ければ境界内で新規構築する
- **GIVEN** 稼働中の実体が壊れ、契約適合の既存実体が他に無い
- **WHEN** システムがフェイルオーバー先を求める
- **THEN** 調達ポリシー境界の内側で新しい実体を自ら構築(例: Dockerfile から再構築)し、契約適合テストで検証してから実行を継続する

#### Scenario: 承認済みセット内フェイルオーバーは特別扱いしない
- **GIVEN** 同一契約に適合する実体が複数登録されている状態
- **WHEN** 一つが壊れて別へ切り替える
- **THEN** これは契約適合実体が複数ある場合の自然な帰結であり、旧案の「承認済み環境セット内フェイルオーバー」を別機構として設けることなく本設計に吸収される
