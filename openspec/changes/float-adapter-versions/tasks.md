# Tasks: float-adapter-versions

## Track A. identity から adapter_version を除外

- [ ] A1 `ModelCandidate.key` の正規化ハッシュ本体から `adapter_version` を除く
  - Spec: FAV-01 / 受け入れ: 版違いで key 同一
- [ ] A2 key 接頭辞 `f"{adapter}@{adapter_version}:{digest}"` から `adapter_version` を除く
  - Spec: FAV-01 / 受け入れ: 接頭辞に版が現れない
- [ ] A3 identity 除外後も candidate の一意性が保たれることを確認する
  - Spec: FAV-01 / 受け入れ: 構成違いは別 key

## Track B. 宣言は種別まで + 最低要求版(任意)

- [ ] B1 candidate 宣言をアダプタ種別(`claude-code` / `codex-cli`)までに限定する
  - Spec: FAV-02 / 受け入れ: 正確な版を宣言しない
- [ ] B2 最低要求版(任意)の宣言を定義し、identity ハッシュに含めないことを確認する
  - Spec: FAV-02 / 受け入れ: 最低要求版の引き上げで identity 不変

## Track C. 実行ごとの実版検証・記録

- [ ] C1 PilotHost が起動時・実行時に CLI 実バージョンを取得する
  - Spec: FAV-03 / 受け入れ: 実版の実測
- [ ] C2 receipt へ `actual_adapter_version` を記録する(既存 `actual_model` と並ぶ実測フィールド)
  - Spec: FAV-03 / 受け入れ: 成功 receipt に実版が入る
- [ ] C3 実版取得不能時は fail-fast し、`null` の成功 receipt を出さない
  - Spec: FAV-03 / 受け入れ: 実測不能で fail-fast
- [ ] C4 最低要求版が宣言されている場合、実版が満たすことを実行前に検証する
  - Spec: FAV-03 / 受け入れ: 最低要求版未達で fail-fast

## Track D. 破壊的変更検知の責務整理

- [ ] D1 厳密バージョン一致検査(`candidate.adapter_version != self.adapter_version`)を撤去する
  - Spec: FAV-04 / 受け入れ: 版照合ゲートの不在
- [ ] D2 破壊的変更検知が preflight・モデル照合・スキーマ検証で担われることを確認する
  - Spec: FAV-04 / 受け入れ: 実挙動検証への責務移譲

## Track E-bis. CLI 版変化の検知と宣言メタデータの自動追従更新

- [ ] Eb1 CLI 実バージョン変化を起動時比較・実行時 `actual_adapter_version` 差分で検知する
  - Spec: FAV-06 / 受け入れ: 版変化の検知
- [ ] Eb2 追従して安全な宣言メタデータを決定論的に書き換え、Ledger へ記録する
  - Spec: FAV-06 / 受け入れ: 決定論的更新 + 監査記録
- [ ] Eb3 決定論的に書けない内容のみエージェンティック更新へフォールバックする
  - Spec: FAV-06 / 受け入れ: フォールバックも監査記録
- [ ] Eb4 preflight(EEP-06)失敗時は自動更新せず fail-fast する(宣言書き換えでの回避禁止)
  - Spec: FAV-06 / 受け入れ: 降格・非互換を宣言書き換えで通さない

## Track E. RouteSnapshot 再発行(承認制・一度きり)

- [ ] E1 identity 除外実装と対象 route の RouteSnapshot 再発行を同一デプロイで切替える
  - Spec: FAV-05 / 受け入れ: 実装と snapshot の同時切替
- [ ] E2 承認制(register → S3* → owner → publish、旧 RETIRE)の枠内で再発行する
  - Spec: FAV-05 / 受け入れ: 承認フロー遵守と単一 routable snapshot

## Track F. EEP 統合

- [ ] F1 実装統合時に FAV-06 の自動更新対象(最低要求版メモ)を EEP-01 の「最低要求 CLI バージョン(任意)」へ結線する
  - Note: `add-execution-environment-profiles` は環境契約 / 環境実体の 2 層へ再設計済みで、EEP-01 は既に「最低要求 CLI バージョン(任意)」を含む
- [x] F2 FAV-06 の検知経路・自動更新トリガを EEP-09(dispatch 時バージョン検証 + preflight キャッシュ)と結線する
  - Spec: FAV-06 / EEP-09 / 受け入れ: dispatch 時キャッシュミスで自動更新が駆動される

## Track G. 検証

- [ ] G1 `openspec validate float-adapter-versions --strict` を通す
