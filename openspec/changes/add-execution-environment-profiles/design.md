# Design: add-execution-environment-profiles

## Context

`pilot-host.json` は実行環境が Windows ネイティブであることを暗黙前提とし、executable 絶対パスとローカルパス allowlist を直書きしている。この前提のため、同一プロファイルを WSL / Docker へ持ち運べず、また codex CLI がプラットフォーム依存でサンドボックスを黙って降格させる挙動を検知できなかった。本 change は実行環境を明示プロファイルとして宣言・束縛の 2 層に分離し、環境差の吸収を PilotHost に集約し、降格を preflight で fail-fast させる。

## 診断で判明した事実(背景)

- codex CLI `0.144.5` は Windows ネイティブで `--sandbox workspace-write` を黙って read-only に降格させる。宣言では workspace-write を要求していても、実際の rollout の `sandbox_policy` は read-only になる。
- この降格により WorkItem 実行が書込拒否になり `ProviderTimeout` で失敗した(execution `e3123604` ほか、2026-07-21)。
- 暫定措置として Windows 実行時のみ `--dangerously-bypass-approvals-and-sandbox` で起動している。Nanihold 側の宣言境界(sandbox-profile の書込ルート制限・`working_directory_allowlist`)は維持しており、緩めていない。

## 2 層分離の設計

### 宣言層(ポータブル)

環境の「意味」を機械非依存で表す。以下を含む。

- `kind`: `native` / `wsl` / `docker`。
- `supported_sandboxes`: この環境が実際に提供できる sandbox モードの集合(例: `["workspace-write", "read-only"]`)。
- 要求 CLI バージョン: codex 等の実行 CLI に要求するバージョン制約。
- パス写像の論理名: 例 `workspace-root`。物理パスではなく論理名のみを宣言する。

`environment_fingerprint` はこの宣言層の正規化ハッシュとして定義する。機械固有情報は一切含めない。スケール時は宣言層を LETHE に版付きアーティファクトとして保存し、PilotHost が起動時にコントロールプレーンから取得する。

### 束縛層(ローカル)

宣言層の論理名を、その機械での実体に束縛する。以下を含む。

- 論理名→機械固有パス(例: `workspace-root = D:\userdata\docs\projects`)。
- CLI 実体パス。
- `CODEX_HOME` 等の環境変数。

束縛層は `environment_fingerprint` に **含めない**。開発時はローカルファイル、スケール時は provisioning / 環境変数で注入する。同一宣言層に対して束縛層が違っても、candidate としては同一である。

## PilotHost の環境吸収責務

PilotHost は宣言層の `kind` と束縛層の実体を突き合わせ、以下を吸収する。

- argv 前置: `kind` に応じて `wsl -d <distro> --` や `docker compose exec <svc>` 等を実行 argv の先頭へ付す。
- パス双方向変換: work_cwd / allowlist / 成果物パスを、ホスト表現とゲスト(WSL / コンテナ)表現の間で往復変換する。
- rollout 読み出し先解決: provider セッション記録(rollout)の実ファイル位置を環境別に解決する。
- エンドポイント URL 書換え: `localhost` と `host.docker.internal` 等を環境別に書き換える。

## Preflight 検証(中核)

PilotHost 起動時に codex を 1 回試走させ、生成された rollout の `sandbox_policy` を読む。これを宣言層の `supported_sandboxes` および要求モードと突き合わせる。

- 一致すれば起動を継続する。
- 不一致(要求 workspace-write に対し実測 read-only 等のサイレント降格)なら **起動を拒否して fail-fast** する。

オーナー評: 「コンテクストを先に注入するよりも合理的」。実行前に環境の実挙動を実測し、宣言と食い違う環境では走らせない。

## 環境切替 = candidate 切替

宣言層が変われば `environment_fingerprint` が変わり、Bayesian routing 上は別 candidate となる。したがって環境切替は RouteSnapshot の承認制(`register → S3_STAR_APPROVED → OWNER_APPROVED → PUBLISHED`、旧版は `superseded_by_approved_snapshot` 理由の human Event で `RETIRED`)の枠内で監査可能に行う。束縛層だけの差し替え(同一機械のパス変更等)は fingerprint を変えず candidate 切替にならない。

## 段階導入

- **Phase 0(既実施)**: Windows ネイティブ時のバイパス暫定措置。宣言境界は維持。
- **Phase 1**: プロファイル機構 + `env:windows-native` / `env:wsl-ubuntu` + preflight を導入。coding 既定を WSL へ切替え、バイパスを撤去する。
- **Phase 2**: `kind: docker` と Mac 側プロファイルを追加。

## 決定と根拠

- **fingerprint は宣言層のみ**: 機械固有情報を含めると同一意味の環境が機械ごとに別 candidate になり、routing の学習が分散する。宣言層のみのハッシュにすることで、意味が同じ環境は同一 candidate に集約され、意味が変わったときだけ candidate が切り替わる。
- **preflight で fail-fast**: サイレント降格は実行時まで気付けず ProviderTimeout として現れる。起動時に 1 回試走して実測することで、失敗を実行前に確定的に検知する。
- **環境吸収は PilotHost に集約**: argv 前置・パス変換・URL 書換えを一箇所に集約し、宣言層をポータブルに保つ。

## リスクと対応

- **preflight の試走コスト**: 起動ごとに codex を 1 回余分に走らせる。1 回限りの軽量試走に限定し、本実行の budget とは別枠にする(policy 変更は本 change のスコープ外)。
- **宣言層スキーマの粒度**: `supported_sandboxes` や要求 CLI バージョンの表現粒度が粗いと降格検知の精度が落ちる。スキーマ粒度はオーナーレビュー論点とする。
- **Phase 1 の切替タイミング**: coding 既定を WSL へ移す時点でバイパスを撤去するため、preflight が WSL で確実に通ることを先に検証する必要がある。切替タイミングはオーナー承認事項とする。
