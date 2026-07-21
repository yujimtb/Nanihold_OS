# Change Proposal: float-adapter-versions

**Version:** 1.0
**Date:** 2026-07-21
**Status:** Proposed
**Repository:** Nanihold_OS
**Type:** candidate identity の構成変更(adapter_version の identity 除外 + 実版の実行時検証・記録)
**Source:** オーナー決定(2026-07-21、LETHE 決定台帳 sup:8d4e2f90)

---

## Why

- 2026-07-21 朝、Claude Code CLI が `2.1.215 → 2.1.216` へ自動更新され、PilotHost の厳密バージョン一致検査(`ClaudeAdapter._validate_version` / `vsm/pilot/claude.py` の `candidate.adapter_version != self.adapter_version` 照合)が起動を拒否した。CLI を `2.1.215` へピン留めする暫定対応を実施したが、オーナーによれば版不一致での停止は今回が初めてではない。
- `adapter_version` は candidate identity ハッシュ(`vsm/pilot/models.py` の `ModelCandidate.key`)の構成要素であり、キー接頭辞 `f"{adapter}@{adapter_version}:{digest}"` と正規化ハッシュ本体の両方に含まれている。したがって版が上がるたびに candidate 更新と RouteSnapshot 再発行が必要になる。この設計は CLI の自動更新頻度に対して運用に耐えない。
- オーナー決定(2026-07-21、LETHE 決定台帳 sup:8d4e2f90):「バージョンは固定せず自動追従。実行ごとに実際の版を検証・記録」。これはモデル選択で承認済みの `provider_configured` + `actual_model` 記録(選択は種別まで宣言し、実体は実行時に実測・記録して突き合わせる)と同じ哲学である。

本 change は、adapter_version を candidate identity から外し、代わりに実行ごとに CLI の実バージョンを実測・記録する方式を仕様化する。破壊的変更の検知はバージョン照合ではなく既存の実挙動検証(preflight・モデル検証・スキーマ検証)が担う、という責務整理も併せて明記する。

## What Changes

- **CHANGED(identity 構成):** `adapter_version` を candidate identity から除外する。`ModelCandidate.key` の正規化ハッシュ本体・キー接頭辞のいずれからも `adapter_version` を除く。これにより同一アダプタ種別・同一構成の candidate は CLI 版が変わっても同一 identity を保つ。
- **CHANGED(candidate 宣言):** candidate の宣言はアダプタ種別(`claude-code` / `codex-cli`)までとし、正確な版は宣言しない。版制約が必要な場合に限り「最低要求版(任意)」のみをオプションとして宣言できる。最低要求版は identity ハッシュには含めない(充足チェックにのみ用いる)。
- **ADDED(実行時の実版検証・記録):** PilotHost は起動時・実行時に CLI の実バージョンを取得し、receipt へ `actual_adapter_version` として記録する。実バージョンの取得に失敗した場合は fail-fast する(`null` のまま成功扱いにしない)。最低要求版が宣言されている場合は実版がそれを満たすことを検証する。
- **ADDED(破壊的変更検知の責務整理):** 破壊的変更の検知はバージョン照合ではなく実挙動検証(起動時 preflight・要求/実測モデル照合・スキーマ検証)が担う。厳密バージョン一致検査は撤去する。責務の対応関係は `design.md` に明記する。
- **ADDED(CLI 版変化の検知と宣言メタデータの自動追従更新):** PilotHost は CLI の実バージョン変化(起動時比較・実行時 `actual_adapter_version` 差分・**dispatch 直前の決定論的バージョン読み取り**(mtime 比較、EEP-09))を検知し、追従して安全な宣言メタデータ(環境契約等の既知版・最低要求版のメモ、モデルメタデータキャッシュ等)を自動更新する。dispatch 時の検証タプル不一致(キャッシュミス)では EEP-09 の preflight 試走に続けて本自動更新が駆動される。更新は決定論的方式(機械的書き換え + Ledger 記録)を優先し、決定論的に書けない内容(挙動差の要約等)のみエージェンティック更新へフォールバックする。更新は監査可能なイベントとして記録する。preflight(EEP-06)失敗時は自動更新せず fail-fast し、降格・非互換を宣言書き換えで通すことは禁止する。
- **CHANGED(EEP 整合):** `add-execution-environment-profiles` は環境契約(EnvironmentContract)/ 環境実体(EnvironmentInstance)の 2 層へ再設計され、EEP-01 の「要求 CLI バージョン」は本 change の方針に合わせて「最低要求 CLI バージョン(任意)」として環境契約に含まれる。その最低要求版のメモが FAV-06 の自動追従更新の対象になる。EEP-09(dispatch 時バージョン検証 + preflight キャッシュ)は FAV-06 の検知経路と自動更新のトリガを共有する。

## Non-Goals

- CLI 自動更新そのものの制御(ピン留め・更新抑止・更新スケジュール)は扱わない。
- モデル選択方式(`provider_configured` / `actual_model` の記録、route の候補構成)の変更は扱わない。

## Affected Invariants

- candidate identity の一意性は維持する。ただし identity の構成が変わる(`adapter_version` を除外する)ため、既存 candidate の key は本 change 適用時に一度だけ変わる。これに伴い、対象 route(interface / coding)について RouteSnapshot を一度だけ再発行する必要がある。再発行は既存の承認制(`register → S3_STAR_APPROVED → OWNER_APPROVED → PUBLISHED`、旧 `PUBLISHED` は `superseded_by_approved_snapshot` 理由の human Event で `RETIRED`)の枠内で行い、同一 `route_key` で routable な snapshot は一つだけという不変条件を維持する。
- receipt の成功要件は強まる。成功 receipt は `actual_adapter_version` を必ず持たなければならず、`null` の成功 receipt は許さない(実測不能は fail-fast)。
- 宣言境界(sandbox-profile の書込ルート制限・`working_directory_allowlist`)は本 change を通じて緩めない。

## Rollout

identity 構成変更のため、対象 route の RouteSnapshot 再発行が一度だけ必要になる。再発行は承認制の枠内で行い、旧 `PUBLISHED` を明示 `RETIRED` にしてから後継を `PUBLISHED` にする。再発行のタイミングはオーナー承認事項とし、`adapter_version` 除外を反映したコード実装と同一デプロイで切替える(実装だけ先行し古い identity のまま routable な snapshot が残る状態、あるいは新 identity の candidate に対応する PUBLISHED snapshot が無い状態を作らない)。EEP 側の最低要求版への調整は実装統合時に併せて行う。
