# Tasks: add-execution-environment-profiles

## Track A. 環境契約 / 環境実体の 2 層定義

- [x] A1 環境契約(EnvironmentContract)スキーマを定義する(能力要件 / `supported_sandboxes` / 最低要求 CLI バージョン(任意) / パス論理名。具体名列挙なし)
  - Spec: EEP-01 / 受け入れ: 能力要件で表され機械固有情報・実行場所名を含まないことの確認
- [ ] A2 環境実体(EnvironmentInstance)スキーマを定義する(論理名→機械固有パス / CLI 実体パス / `CODEX_HOME`)
  - Spec: EEP-03 / 受け入れ: 実体識別情報が identity ハッシュに含まれないことの確認
- [x] A3 `environment_fingerprint` を環境契約の正規化ハッシュとして定義する
  - Spec: EEP-02 / 受け入れ: 実体変更で fingerprint 不変

## Track B. 環境契約アーティファクトの保管

- [x] B1 環境契約を LETHE の版付きアーティファクトとして保存する経路を定義する
  - Spec: EEP-04 / 受け入れ: 版付き保存の確認
- [ ] B2 PilotHost 起動時にコントロールプレーンから環境契約を取得する
  - Spec: EEP-04 / 受け入れ: 起動時取得の確認

## Track C. PilotHost 環境吸収

- [ ] C1 実体種別に応じた argv 前置(`wsl -d <distro> --` / `docker compose exec` 等)を実装する
  - Spec: EEP-05 / 受け入れ: argv 前置の確認
- [ ] C2 work_cwd / allowlist / 成果物パスの双方向変換を実装する
  - Spec: EEP-05 / 受け入れ: 往復変換の確認
- [ ] C3 rollout 読み出し先解決とエンドポイント URL 書換え(`localhost` vs `host.docker.internal`)を実装する
  - Spec: EEP-05 / 受け入れ: URL 書換えの確認

## Track D. Preflight = 契約適合テスト(中核)

- [x] D1 codex を 1 回試走させ rollout の `sandbox_policy` と契約の他能力要件を実測する
  - Spec: EEP-06 / 受け入れ: 契約適合の実測
- [ ] D2 契約に不適合(サイレント降格等)で当該実体の実行を拒否・fail-fast し、合格実体を instance fingerprint 付きで Operational Ledger に記録する
  - Track B: fail-fast、証拠生成、記録フックまで実装。Ledger 接続は Track C の残作業。
  - Spec: EEP-06 / 受け入れ: 不適合時の拒否 + 合格実体の記録

## Track D-bis. dispatch 時バージョン検証 + preflight キャッシュ

- [x] Db1 dispatch 直前に CLI 実バージョンを決定論的に読む(バージョンファイル + mtime 比較、プロセス起動なし)
  - Spec: EEP-09 / 受け入れ: プロセス起動を伴わない読み取り
- [x] Db2 検証タプル(CLI バージョン × sandbox モード × `environment_fingerprint`)一致で preflight をスキップする
  - Spec: EEP-09 / 受け入れ: キャッシュヒットで試走スキップ
- [x] Db3 タプル不一致で preflight 試走 + 宣言メタデータ自動更新(FAV-06)を行ってから実行する
  - Spec: EEP-09 / 受け入れ: キャッシュミスで試走 + 自動更新
- [x] Db4 検証結果キャッシュを Ledger または PilotHost ローカルに永続化し再起動を跨いで有効にする
  - Spec: EEP-09 / 受け入れ: 再起動後もキャッシュ有効
- [x] Db5 キャッシュミス時の preflight 失敗は宣言書き換えで回避せず fail-fast する
  - Spec: EEP-09 / 受け入れ: 降格・非互換を宣言書き換えで通さない

## Track E. 契約変更 = candidate 切替 / 実体フェイルオーバーは candidate 不変

- [ ] E1 契約変更 → `environment_fingerprint` 変更 → 別 candidate の連鎖を確認する
  - Spec: EEP-07 / 受け入れ: 別 candidate 化
- [ ] E2 RouteSnapshot 承認制(register → S3* → owner → publish、旧 RETIRE)の枠内で切替える
  - Spec: EEP-07 / 受け入れ: 承認フロー遵守
- [ ] E3 同一契約を満たす実体間のフェイルオーバーで fingerprint が不変・RouteSnapshot 再発行不要を確認する
  - Spec: EEP-07 / 受け入れ: 実体切替で candidate 不変

## Track F. オーナー承認境界と S3 自律ライフサイクル

- [x] F1 オーナー(S5)の承認対象を環境契約 + 調達ポリシー境界に限定する
  - Spec: EEP-10 / 受け入れ: 個別実体操作がオーナー承認対象外
- [ ] F2 S3 が契約適合実体を発見・構築・検証・廃棄し合格実体を Operational Ledger へ記録する
  - Spec: EEP-11 / 受け入れ: 合格実体の instance fingerprint 付き記録
- [ ] F3 実体破損時のフェイルオーバー(別適合実体選択 / 境界内での新実体構築)を承認不要・非同期通知で実装する
  - Spec: EEP-12 / 受け入れ: 承認不要のフェイルオーバー + 境界内再構築

## Track G. 段階導入

- [ ] G1 Phase 1: 契約/実体機構 + 契約適合 preflight + dispatch 時検証/キャッシュを導入し、初期実体(Windows ネイティブ / WSL)を契約適合として登録する
  - Spec: EEP-08
- [ ] G2 Phase 1: WSL 実体で preflight が通ることを検証してから coding 既定を WSL へ切替え、バイパスを撤去する
  - Spec: EEP-08 / 受け入れ: バイパス撤去前の preflight 検証
- [ ] G3 Phase 2: docker 実体と Mac 側実体を同一契約に適合する新実体として追加する
  - Spec: EEP-08

## Track H. 検証

- [ ] H1 `openspec validate add-execution-environment-profiles --strict` を通す
