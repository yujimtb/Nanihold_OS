# Tasks: add-execution-environment-profiles

## Track A. プロファイル 2 層の定義

- [ ] A1 宣言層(ポータブル)スキーマを定義する(`kind` / `supported_sandboxes` / 要求 CLI バージョン / パス論理名)
  - Spec: EEP-01 / 受け入れ: 機械固有情報を含まないことの確認
- [ ] A2 束縛層(ローカル)スキーマを定義する(論理名→機械固有パス / CLI 実体パス / `CODEX_HOME`)
  - Spec: EEP-03 / 受け入れ: identity ハッシュに含まれないことの確認
- [ ] A3 `environment_fingerprint` を宣言層の正規化ハッシュとして定義する
  - Spec: EEP-02 / 受け入れ: 束縛層変更で fingerprint 不変

## Track B. 宣言層アーティファクトの保管

- [ ] B1 宣言層を LETHE の版付きアーティファクトとして保存する経路を定義する
  - Spec: EEP-04 / 受け入れ: 版付き保存の確認
- [ ] B2 PilotHost 起動時にコントロールプレーンから宣言層を取得する
  - Spec: EEP-04 / 受け入れ: 起動時取得の確認

## Track C. PilotHost 環境吸収

- [ ] C1 `kind` に応じた argv 前置(`wsl -d <distro> --` / `docker compose exec` 等)を実装する
  - Spec: EEP-05 / 受け入れ: argv 前置の確認
- [ ] C2 work_cwd / allowlist / 成果物パスの双方向変換を実装する
  - Spec: EEP-05 / 受け入れ: 往復変換の確認
- [ ] C3 rollout 読み出し先解決とエンドポイント URL 書換え(`localhost` vs `host.docker.internal`)を実装する
  - Spec: EEP-05 / 受け入れ: URL 書換えの確認

## Track D. Preflight 検証(中核)

- [ ] D1 起動時に codex を 1 回試走させ rollout の `sandbox_policy` を読む
  - Spec: EEP-06 / 受け入れ: 試走の実測
- [ ] D2 宣言の `supported_sandboxes` / 要求モードと突き合わせ、不一致で起動拒否・fail-fast する
  - Spec: EEP-06 / 受け入れ: サイレント降格時の起動拒否

## Track E. 環境切替の承認制

- [ ] E1 宣言層変更 → `environment_fingerprint` 変更 → 別 candidate の連鎖を確認する
  - Spec: EEP-07 / 受け入れ: 別 candidate 化
- [ ] E2 RouteSnapshot 承認制(register → S3* → owner → publish、旧 RETIRE)の枠内で切替える
  - Spec: EEP-07 / 受け入れ: 承認フロー遵守

## Track F. 段階導入

- [ ] F1 Phase 1: プロファイル機構 + `env:windows-native` / `env:wsl-ubuntu` + preflight を導入する
  - Spec: EEP-08
- [ ] F2 Phase 1: WSL で preflight が通ることを検証してから coding 既定を WSL へ切替え、バイパスを撤去する
  - Spec: EEP-08 / 受け入れ: バイパス撤去前の preflight 検証
- [ ] F3 Phase 2: `kind: docker` と Mac 側プロファイルを追加する
  - Spec: EEP-08

## Track G. 検証

- [ ] G1 `openspec validate add-execution-environment-profiles --strict` を通す
