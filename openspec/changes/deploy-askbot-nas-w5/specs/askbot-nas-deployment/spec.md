## ADDED Requirements

### Requirement: NAS-01 前提条件 — RAM 増設完了(16GiB 以上)を本番配備ゲートとする
システムは ask-bot の NAS 本番配備の前提条件として、対象 NAS(Synology DS920+)の RAM 増設完了(16GiB 以上)を満たさなければならない(SHALL)。RAM 増設が未完了(実測 3.7GiB 等、16GiB 未満)の間は本番配備を開始してはならない(SHALL NOT)。増設完了までは挙動調査を試験環境で先行しなければならない(SHALL)。

#### Scenario: RAM 未装着では本番配備を開始しない
- **GIVEN** 増設用 RAM は購入済みだが未装着で、実測メモリが 16GiB 未満(例: 3.7GiB)である
- **WHEN** ask-bot の NAS 本番配備を開始しようとする
- **THEN** 前提条件未達として本番配備を開始せず、試験環境での挙動調査に留める

#### Scenario: RAM 増設完了後に本番配備を許可する
- **GIVEN** RAM 増設(16GiB 以上)の装着が完了している
- **WHEN** 本番配備の前提条件を評価する
- **THEN** RAM 前提が満たされ、他の配備規約(NAS-02〜NAS-05)に沿って本番配備を進められる

### Requirement: NAS-02 メモリ設計 — 総予算 16GiB で最低保証 + 上限による動的調整
システムはメモリ設計を総予算 16GiB を前提として行わなければならない(SHALL)。各モジュールには最低保証(`deploy.resources.reservations.memory`)と上限(`deploy.resources.limits.memory`)を設定し、上限の範囲内で動的に調整しなければならない(SHALL)。各モジュールの最低保証の総和は総予算 16GiB を超えてはならない(SHALL NOT)。

#### Scenario: 各モジュールが reservations と limits を持つ
- **GIVEN** ask-bot の Compose スタックの各モジュール
- **WHEN** その `deploy.resources` を確認する
- **THEN** 最低保証 `reservations.memory` と上限 `limits.memory` が設定され、上限内で動的調整される

#### Scenario: 最低保証の総和が総予算を超えない
- **GIVEN** 各モジュールの `reservations.memory` の合計
- **WHEN** メモリ設計を検証する
- **THEN** 合計が総予算 16GiB を超えず、予算内に収まる

### Requirement: NAS-03 配備規約 — 配置パス・named volume・機密注入・最小権限
システムは ask-bot を `/volume1/docker/ask-bot/` 配下に配置しなければならない(SHALL)。永続データは named volume で保持しなければならない(SHALL)。機密値は `.env` + `${VAR:?required}` 方式で注入し、必須変数が未設定の場合は起動を失敗させなければならない(SHALL)。機密値をイメージやリポジトリに焼き込んではならない(SHALL NOT)。各コンテナは `read_only`・`cap_drop: ALL`・`no-new-privileges` を適用して最小権限で稼働しなければならない(SHALL)。ルート FS 空きが逼迫(実測 829MB)しているため、イメージ・ボリューム・一時ファイルは `/volume1` 側に置かなければならない(SHALL)。

#### Scenario: 配置と永続化が規約どおり
- **GIVEN** ask-bot の Compose スタック
- **WHEN** その配置と永続化を確認する
- **THEN** `/volume1/docker/ask-bot/` 配下に置かれ、永続データは named volume で保持される

#### Scenario: 必須機密の未設定で起動が失敗する
- **GIVEN** `.env` + `${VAR:?required}` 方式で参照される必須機密変数が未設定である
- **WHEN** スタックを起動しようとする
- **THEN** 未設定の必須変数により起動が失敗し、機密はイメージ/リポジトリに焼き込まれていない

#### Scenario: コンテナが最小権限で稼働する
- **GIVEN** ask-bot の各コンテナ定義
- **WHEN** そのセキュリティ設定を確認する
- **THEN** `read_only`・`cap_drop: ALL`・`no-new-privileges` が適用されている

### Requirement: NAS-04 データ移送 — tar 圧縮ストリームの SSH 直送
システムはデータ移送を tar 圧縮ストリームの SSH 直送で行わなければならない(SHALL)。移送は中間の平文一時ファイルをルート FS 上に滞留させない形で行わなければならない(SHALL)。

#### Scenario: tar 圧縮ストリームを SSH 直送する
- **GIVEN** 移送対象のデータ
- **WHEN** データを NAS へ移送する
- **THEN** tar 圧縮ストリームが SSH 経由で直送され、逼迫したルート FS 上に大きな一時ファイルを滞留させない

### Requirement: NAS-05 ポート競合回避 — ネイティブ PostgreSQL(127.0.0.1:5432)を避ける
システムは ask-bot の DB ポートを、ホスト上のネイティブ PostgreSQL が占有する `127.0.0.1:5432` と競合しないよう別ポートまたは別バインドで公開しなければならない(SHALL)。`127.0.0.1:5432` へ重ねてバインドしてはならない(SHALL NOT)。

#### Scenario: 5432 と競合しないよう公開する
- **GIVEN** ホストのネイティブ PostgreSQL が `127.0.0.1:5432` を占有している
- **WHEN** ask-bot の DB ポートを公開する
- **THEN** 別ポートまたは別バインドで公開され、`127.0.0.1:5432` と競合しない

### Requirement: NAS-06 試験環境での先行挙動調査フェーズ
システムは本番配備の前に、試験環境で ask-bot の挙動調査を行うフェーズを実施しなければならない(SHALL)。RAM 増設完了(NAS-01)までは本番配備を行わず、挙動調査は試験環境で先行しなければならない(SHALL)。試験環境での調査結果は本番配備の前提評価に反映しなければならない(SHALL)。

#### Scenario: 本番前に試験環境で挙動を調査する
- **GIVEN** RAM 増設が未完了で本番配備が保留されている
- **WHEN** ask-bot の挙動を確認する
- **THEN** 試験環境で先行して挙動調査を行い、その結果を本番配備の前提評価へ反映する
