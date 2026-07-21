## ADDED Requirements

### Requirement: FAV-01 adapter_version の candidate identity 除外
システムは candidate identity(`ModelCandidate.key`)の計算から `adapter_version` を除外しなければならない(SHALL)。key の正規化ハッシュ本体・key 接頭辞のいずれにも `adapter_version` を含めてはならない(SHALL NOT)。同一アダプタ種別・同一構成の candidate は CLI の実バージョンが異なっても同一 identity を持たなければならない(SHALL)。

#### Scenario: 版違いで identity が変わらない
- **GIVEN** アダプタ種別・provider・selection・toolset・sandbox/environment fingerprint が同一で、CLI の実バージョンだけが異なる 2 つの実行
- **WHEN** それぞれの candidate identity(key)を計算する
- **THEN** 両者の key は一致し、同一 candidate として扱われる

#### Scenario: key 接頭辞に版が現れない
- **GIVEN** ある candidate
- **WHEN** その key を確認する
- **THEN** key の接頭辞・ハッシュ本体のいずれにも `adapter_version` が現れない

#### Scenario: 構成が違えば別 identity
- **GIVEN** 同一アダプタ種別だが toolset(または provider / selection / fingerprint)が異なる 2 つの candidate
- **WHEN** それぞれの key を計算する
- **THEN** 両者の key は一致せず、別 candidate として扱われる

### Requirement: FAV-02 宣言はアダプタ種別まで + 最低要求版(任意)
システムは candidate の宣言をアダプタ種別(`claude-code` / `codex-cli`)までに限定しなければならない(SHALL)。candidate は正確な CLI バージョンを宣言してはならない(SHALL NOT)。版制約が必要な場合に限り、candidate は「最低要求版」を任意で宣言できる(MAY)。最低要求版は充足チェックにのみ用い、identity ハッシュに含めてはならない(SHALL NOT)。

#### Scenario: 宣言は種別までで正確な版を持たない
- **GIVEN** ある candidate の宣言
- **WHEN** その内容を確認する
- **THEN** アダプタ種別は宣言されているが、正確な CLI バージョンは宣言されていない

#### Scenario: 最低要求版を引き上げても identity が変わらない
- **GIVEN** 最低要求版を任意宣言した candidate
- **WHEN** 最低要求版だけを引き上げて key を再計算する
- **THEN** identity(key)は変わらず、同一 candidate であり続ける

### Requirement: FAV-03 実行ごとの実版検証・記録
システムは PilotHost に、起動時・実行時に CLI の実バージョンを取得し receipt へ `actual_adapter_version` として記録させなければならない(SHALL)。実バージョンの取得に失敗した場合、システムは fail-fast しなければならず(SHALL)、`actual_adapter_version = null` のまま成功 receipt を発行してはならない(SHALL NOT)。最低要求版が宣言されている場合、システムは実版がそれを満たすことを実行前に検証し、満たさなければ fail-fast しなければならない(SHALL)。

#### Scenario: 成功 receipt に実版が記録される
- **GIVEN** CLI の実バージョンを取得できる実行
- **WHEN** 実行が成功して receipt を発行する
- **THEN** receipt は `actual_adapter_version` に実測した版を持つ

#### Scenario: 実版取得不能で fail-fast する
- **GIVEN** CLI の実バージョンを取得できない実行
- **WHEN** PilotHost が実版を取得しようとする
- **THEN** fail-fast し、`actual_adapter_version = null` の成功 receipt は発行しない

#### Scenario: 最低要求版未達で fail-fast する
- **GIVEN** 最低要求版を宣言した candidate に対し、実版がそれを下回る環境
- **WHEN** PilotHost が実行前に実版を検証する
- **THEN** 最低要求版未達として fail-fast し、本実行を開始しない

### Requirement: FAV-04 破壊的変更検知の責務を実挙動検証へ移譲
システムは破壊的変更の検知をバージョン照合ではなく実挙動検証(起動時 preflight・要求/実測モデル照合・スキーマ検証)に担わせなければならない(SHALL)。システムは厳密バージョン一致検査(`candidate.adapter_version` と稼働 CLI 版の一致要求)を検知手段として用いてはならない(SHALL NOT)。

#### Scenario: 版照合ゲートが存在しない
- **GIVEN** 稼働 CLI が candidate 宣言時と異なる版へ自動更新された環境
- **WHEN** PilotHost が実行を開始する
- **THEN** 版番号の不一致のみを理由に起動を拒否せず、実挙動検証(preflight・モデル照合・スキーマ検証)で破壊的差異を判定する

#### Scenario: 実挙動の破壊的差異は検知される
- **GIVEN** 版は許容範囲だが実挙動(sandbox 降格・モデル差異・スキーマ不整合)が宣言と食い違う環境
- **WHEN** preflight・モデル照合・スキーマ検証を評価する
- **THEN** 該当する実挙動検証が不一致を検知し fail-fast する

### Requirement: FAV-05 identity 変更に伴う一度きりの RouteSnapshot 再発行
システムは `adapter_version` の identity 除外に伴う candidate key の変更を、対象 route の RouteSnapshot 再発行として反映しなければならない(SHALL)。再発行は承認制(`register → S3_STAR_APPROVED → OWNER_APPROVED → PUBLISHED`、旧 `PUBLISHED` は `superseded_by_approved_snapshot` 理由の human Event で `RETIRED`)の枠内で行わなければならない(SHALL)。identity 除外の実装と RouteSnapshot 再発行は同一デプロイで切替えなければならず(SHALL)、旧版 retirement と後継 publish を一操作へまとめてはならない(SHALL NOT)。

#### Scenario: 実装と snapshot を同時に切替える
- **GIVEN** identity から `adapter_version` を除いた実装
- **WHEN** 対象 route(interface / coding)の後継 RouteSnapshot を切替える
- **THEN** 実装と RouteSnapshot 再発行が同一デプロイで切替わり、旧 identity の candidate を指す PUBLISHED snapshot も新 identity に対応しない実装も残らない

#### Scenario: 承認制の枠内で再発行し単一 routable snapshot を保つ
- **GIVEN** 新 identity の candidate に基づく後継 snapshot 候補
- **WHEN** register から S3* 承認・owner 承認を経て publish する
- **THEN** 旧 PUBLISHED snapshot が先に RETIRED になってから後継が PUBLISHED になり、同一 `route_key` で routable な snapshot は一つだけに保たれる
