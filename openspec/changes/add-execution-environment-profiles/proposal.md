# Change Proposal: add-execution-environment-profiles

**Version:** 1.0
**Date:** 2026-07-21
**Status:** Proposed
**Repository:** Nanihold_OS
**Type:** ExecutionEnvironment プロファイル機構の新設(candidate identity・PilotHost 実行境界に影響)
**Source:** オーナー合意設計(2026-07-21)

---

## Why

エージェント実行環境の暗黙前提と、codex CLI のサンドボックス挙動の食い違いにより、WorkItem 実行が失敗した。

- codex CLI `0.144.5` は Windows ネイティブ環境で `--sandbox workspace-write` を **黙って read-only へ降格** させる。この降格により WorkItem 実行が書込拒否となり、`ProviderTimeout` で失敗した(2026-07-21、execution `e3123604` ほか)。
- 現在は暫定措置として、Windows 実行時のみ codex を `--dangerously-bypass-approvals-and-sandbox` で起動している。ただし宣言境界(Nanihold の sandbox-profile が定める書込ルート制限と `working_directory_allowlist`)は維持しており、境界そのものは緩めていない。
- `pilot-host.json` は実行環境が Windows ネイティブであることを暗黙の前提としており、executable の絶対パスやローカルパスの allowlist が直書きされている。したがって同一プロファイルを別環境(WSL / Docker)へ持ち運べない。
- オーナー方針: エージェントは Nanihold が動く環境を問わず実行可能であるべきである。開発は極力 WSL / Docker で行う(Mac 共同開発者とのパリティ確保)。

これらを解決するため、実行環境を明示プロファイルとして宣言・束縛の 2 層に分離し、PilotHost に環境吸収責務を持たせ、起動時 preflight で「サイレント降格」を検知して fail-fast する機構を仕様化する。

## What Changes

- **ADDED:** `ExecutionEnvironment` プロファイルを **宣言層(ポータブル)** と **束縛層(ローカル)** の 2 層に分離して定義する。宣言層は `kind`(native / wsl / docker)・`supported_sandboxes`・要求 CLI バージョン・パス写像の論理名(例: `workspace-root`)を持つ。束縛層は論理名→機械固有パス・CLI 実体パス・`CODEX_HOME` 等を持つ。
- **ADDED:** candidate identity の `environment_fingerprint` を、**宣言層の正規化ハッシュ**として正式定義する。機械固有情報(束縛層)はハッシュに含めない。
- **ADDED:** 宣言層アーティファクトのスケール時保管を規定する。LETHE に版付きアーティファクトとして保存し、PilotHost が起動時にコントロールプレーンから取得する。
- **ADDED:** PilotHost の **環境吸収責務** を規定する。`kind` に応じた argv 前置(`wsl -d <distro> --`、`docker compose exec` 等)、work_cwd / allowlist / 成果物パスの双方向変換、provider セッション記録(rollout)の読み出し先解決、エンドポイント URL(`localhost` vs `host.docker.internal`)の環境別書換え。
- **ADDED:** 起動時 **Preflight 検証** を規定する。PilotHost が codex を 1 回試走させ、rollout の `sandbox_policy` が宣言の `supported_sandboxes` / 要求モードと一致するか突き合わせ、不一致(サイレント降格)は起動拒否で fail-fast する。
- **ADDED:** **環境切替 = candidate 切替** を規定する。宣言層が変われば `environment_fingerprint` が変わり別 candidate となり、RouteSnapshot の承認制(`register → S3_STAR_APPROVED → OWNER_APPROVED → PUBLISHED`、旧版は `RETIRED`)の枠内で監査可能に切り替わる。
- **ADDED:** 段階導入(Phase 0 / Phase 1 / Phase 2)を規定する。Phase 0 は現行バイパス暫定措置(既実施)、Phase 1 でプロファイル機構と preflight を導入し coding 既定を WSL へ切替えてバイパスを撤去、Phase 2 で `kind: docker` と Mac 側プロファイルを追加する。

## Non-Goals

- codex CLI 自体の sandbox 実装の修正(サイレント降格そのものを CLI 側で直すことはしない)。
- Claude Code 側アダプタの同時改修(将来の follow-up とする)。
- 予算・タイムアウト政策(`token_budget` / 秒数制限)の変更。

## Affected Invariants

candidate identity の一意性は維持する。`environment_fingerprint` は宣言層のみから決まり、束縛層(機械固有パス等)を変えても同一 candidate であり続ける。RouteSnapshot は承認制であり、同一 `route_key` で routable な snapshot は一つだけという不変条件を維持する。宣言境界(sandbox-profile の書込ルート制限と `working_directory_allowlist`)は本 change を通じて緩めない。

## Rollout

Phase 0 は既実施(Windows ネイティブ時のバイパス暫定措置)。Phase 1 の coding 既定切替(WSL へ)とバイパス撤去は、プロファイル機構・preflight の実装完了とオーナー承認を経て実施する。環境切替は RouteSnapshot 承認制の枠内で行い、旧版を明示 `RETIRED` にしてから後継を `PUBLISHED` にする。Phase 2(Docker / Mac プロファイル)は Phase 1 安定後に着手する。
