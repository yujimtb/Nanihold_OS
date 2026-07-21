# Change Proposal: deploy-askbot-nas-w5

**Version:** 1.0
**Date:** 2026-07-21
**Status:** Proposed
**Repository:** Nanihold_OS
**Type:** ask-bot の NAS(Synology DS920+)本番配備の前提条件・メモリ設計・配備規約の仕様化(W5)
**Source:** NAS 実地調査(2026-07-21)結果とオーナー確認(RAM 増設完了までは本番配備を保留、挙動調査は試験環境で先行)。

---

## Why

2026-07-21 の NAS 実地調査で、ask-bot の配備先である Synology DS920+ の実態が判明した。

- ハードウェア/OS: Synology DS920+ / DSM 7.3.2 / Docker 24.0.2 + Compose v2.20.1。
- メモリ: RAM 実測 3.7GiB + swap 4.2GiB。増設用 RAM は購入済みだが未装着。
- ストレージ: `/volume1` 空き 8.7TB。ただしルート FS 空きは 829MB のみ。
- ネットワーク: Tailscale 直結。
- 既存状態: 旧デプロイ物は完全撤去済み。ネイティブ PostgreSQL が `127.0.0.1:5432` を占有している。

オーナー確認: RAM は購入済みだが未装着であり、装着完了までは NAS 本番配備を保留する。挙動調査は試験環境で先行する。

現状の実測 RAM(3.7GiB)は ask-bot の全モジュールを安定稼働させるには不足であり、また `5432` のポート競合・ルート FS の逼迫(829MB)など、配備前に仕様として固めるべき前提と規約が複数ある。本 change はこれらを NAS 配備の前提条件・メモリ設計・配備規約・データ移送・試験先行フェーズとして仕様化する。

## What Changes

- **ADDED(前提条件 / 本番配備ゲート):** NAS 本番配備の前提条件として RAM 増設完了(16GiB 以上)を明記する。装着完了までは本番配備を保留し、試験環境での挙動調査を先行する(NAS-01)。
- **ADDED(メモリ設計):** 総予算 16GiB を前提に、各モジュールへ最低保証(`deploy.resources.reservations.memory`)と上限(`deploy.resources.limits.memory`)を設定し、範囲内で動的調整する設計を規定する(NAS-02)。
- **ADDED(配備規約):** 配置は `/volume1/docker/ask-bot/` 配下、永続データは named volume、機密は `.env` + `${VAR:?required}` 方式、コンテナは `read_only` + `cap_drop: ALL` + `no-new-privileges` を規定する(NAS-03)。
- **ADDED(データ移送):** データ移送は tar 圧縮ストリームの SSH 直送で行うことを規定する(NAS-04)。
- **ADDED(ポート競合回避):** ネイティブ PostgreSQL が `127.0.0.1:5432` を占有しているため、ask-bot 側の DB ポートは競合しないよう別ポート/別バインドで公開することを規定する(NAS-05)。
- **ADDED(試験環境での先行挙動調査フェーズ):** 本番配備の前に試験環境で挙動調査を行うフェーズを追加する(NAS-06)。

## Non-Goals

- ask-bot アプリケーション自体の機能改修は扱わない。
- RAM の調達・装着そのもの(物理作業)の手順は扱わない。本 change は装着完了を配備ゲートの前提として参照するのみ。
- Tailscale / ネットワーク経路の再設計は扱わない(直結を前提とする)。
- DSM / Docker / Compose のバージョンアップ計画は扱わない(実測版を前提とする)。

## Affected Invariants

- 本番配備は RAM 増設完了(16GiB 以上)を満たすまで開始しない。この前提が満たされない限り、配備は試験環境に限定される。
- 各モジュールのメモリは総予算 16GiB の内側で `reservations`(最低保証)と `limits`(上限)を持ち、総和が予算を超えない。
- 機密値はイメージ/リポジトリに焼き込まず、`.env` + `${VAR:?required}` で注入し、未設定時は起動を失敗させる。
- コンテナは最小権限(`read_only` / `cap_drop: ALL` / `no-new-privileges`)で稼働する。
- ask-bot の DB ポートはホストのネイティブ PostgreSQL(`127.0.0.1:5432`)と競合しない。

## Rollout

RAM 増設(16GiB 以上)の装着が完了するまでは本番配備を保留し、試験環境で挙動調査を先行する(NAS-06)。装着完了を確認した後に、`/volume1/docker/ask-bot/` 配下へ Compose スタックを配置し、メモリ予算・ポート・セキュリティ規約に沿って本番配備する。データ移送は tar 圧縮ストリームの SSH 直送で行う。ルート FS 空きが 829MB と逼迫しているため、イメージ・ボリューム・一時ファイルはすべて `/volume1` 側に置く。
