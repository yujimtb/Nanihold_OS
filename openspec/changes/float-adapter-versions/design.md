# Design: float-adapter-versions

## Context

candidate identity(`vsm/pilot/models.py` の `ModelCandidate.key`)は現在 `adapter_version` を構成要素に含む。key は `f"{adapter}@{adapter_version}:{digest}"` の形を取り、`adapter_version` は接頭辞にも正規化ハッシュ本体(`canonical` の `adapter_version` フィールド)にも入っている。さらに `ClaudeAdapter`(`vsm/pilot/claude.py`)は実行前に `candidate.adapter_version != self.adapter_version` を厳密照合し、不一致で `InvariantViolation` を投げて起動を拒否する。

この設計では CLI の版が上がるたびに candidate の identity が変わり、RouteSnapshot 再発行が必要になる。Claude Code CLI は自動更新されるため(2026-07-21 の `2.1.215 → 2.1.216`)、版不一致による停止が反復して発生している。オーナー決定(2026-07-21、sup:8d4e2f90)は「バージョンは固定せず自動追従。実行ごとに実際の版を検証・記録」であり、モデル選択で確立済みの「宣言は種別まで・実体は実行時に実測して記録・突き合わせる」哲学(`provider_configured` + `actual_model`)を版にも適用する。

## 決定と根拠

### adapter_version を identity から外す

identity は「どの実行意味に対する候補か」を表すべきで、CLI のパッチ版のような実行環境の細部は含めるべきでない。版を identity に含めると、意味的に同一の候補が版ごとに別 candidate へ散らばり、Bayesian routing の学習が版更新のたびにリセットされる。`adapter_version` を key の接頭辞・ハッシュ本体の双方から除くことで、同一アダプタ種別・同一構成は版が変わっても同一 candidate に集約される。

これはモデル選択の `provider_configured`(model_snapshot を identity に固定せず、実行時に `actual_model` を実測して requested と突き合わせる)と同型の設計である。版についても「宣言は種別まで、実体は実行時に実測」へ揃える。

### 宣言は種別まで + 最低要求版(任意)

candidate は `adapter`(`claude-code` / `codex-cli`)まで宣言する。正確な版は宣言しない。ある機能に最低版が要る場合に限り、candidate は「最低要求版」を任意で宣言できる。最低要求版は充足チェック(実版 ≥ 最低要求版)にのみ用い、identity ハッシュには含めない。したがって最低要求版を引き上げても identity は変わらない(routing の学習は保たれる)。最低要求版そのものの変更が候補の意味を変えると判断される場合の扱いは、オーナーレビュー論点とする。

### 実行ごとの実版検証・記録

PilotHost は起動時・実行時に CLI の実バージョンを取得し、receipt へ `actual_adapter_version` として記録する(`vsm/pilot/production_host.py` の receipt。既存の `actual_model` と並ぶ実測フィールドとして追加する)。

- 実版の取得に失敗した場合は fail-fast する。`actual_adapter_version = null` のまま成功 receipt を発行してはならない。これは既存の成功 receipt 不変条件(`actual_model` / `provider_session_id` / `usage` が揃うこと)に `actual_adapter_version` を加えるのと同型である。
- 最低要求版が宣言されている場合は、実版がそれを満たすことを実行前に検証し、満たさなければ fail-fast する。

### 破壊的変更の検知は実挙動検証が担う(責務整理)

厳密バージョン一致検査(`ClaudeAdapter._validate_version` 相当の `candidate.adapter_version != self.adapter_version`)は撤去する。「新版が破壊的か」を版番号の一致で判断するのをやめ、実挙動の検証で判断する。責務の対応は以下のとおり。

- 実行環境・sandbox の破壊的差異 → 起動時 preflight(EEP-06。codex 試走で `sandbox_policy` を実測しサイレント降格を fail-fast)。
- モデルの破壊的差異 → 要求/実測モデル照合(`RequestedActualModelMismatch`。requested と `actual_model` の突き合わせ)。
- 入出力契約の破壊的差異 → スキーマ検証(receipt / レスポンスの構造検証)。

版番号照合はこれらの実挙動検証を代替できない(版が同じでも挙動が変わりうるし、版が変わっても挙動が同じことは多い)。よって版照合ゲートは撤去し、`actual_adapter_version` は監査・回帰調査のための記録として receipt に残す。

## 移行と RouteSnapshot 再発行(レビュー論点)

`adapter_version` を identity から外すと、既存 candidate の key が一度だけ変わる。対象 route(interface / coding)は、新 identity の candidate に対応する後継 RouteSnapshot を承認制で再発行する必要がある。

- 再発行は `register → S3_STAR_APPROVED → OWNER_APPROVED → PUBLISHED` を経て、旧 `PUBLISHED` を `superseded_by_approved_snapshot` 理由の human Event で `RETIRED` にしてから後継を `PUBLISHED` にする。
- タイミング: identity を変えるコード実装(key 計算からの `adapter_version` 除外)と RouteSnapshot 再発行は同一デプロイで切替える。実装だけ先行すると、稼働中の PUBLISHED snapshot が旧 identity の candidate を指したまま新コードでは解決できない状態が生じうる。逆に snapshot だけ先行すると新 identity の candidate に対応するコードが無い。両者を一致させるため、切替は単一の承認済み変更として実施する。
- これは identity 構成変更に伴う一度きりの再発行であり、以後 CLI 版が上がっても再発行は不要になる(それが本 change の目的)。

## リスクと対応

- **監査の連続性**: identity が一度変わるため、旧 candidate と新 candidate の routing 履歴が分断される。分断は一度きりであり、`actual_adapter_version` の記録で版遷移は receipt 側に残るため、回帰調査は receipt から辿れる。旧 candidate の posterior をどこまで引き継ぐか(引き継がない/初期化する)はオーナーレビュー論点とする。
- **最低要求版の粒度**: 最低要求版の表現が粗いと必要な機能差を捉えられない。粒度はオーナーレビュー論点とする。
- **EEP との統合順序**: EEP-01 の「要求 CLI バージョン」を最低要求版へ弱める調整は実装統合時に行う。本 change では EEP の spec 本文を変更せず、`tasks.md` に統合時タスクとして残す。
