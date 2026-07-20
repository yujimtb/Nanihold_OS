# Change Proposal: update-coding-route-luna-first

**Version:** 1.0
**Date:** 2026-07-20
**Status:** Proposed
**Repository:** Nanihold_OS
**Type:** 本番 coding route の候補構成変更(RouteSnapshot 再発行を伴う)
**Source:** オーナー決定(2026-07-20 Q1)

---

## Why

本番の coding route(`route_key = coding:personal-production`)は現在 `gpt-5.6-sol/xhigh` 単独である。オーナー決定(2026-07-20 Q1)により、これを `gpt-5.6-luna/xhigh` を第一候補、`gpt-5.6-sol/xhigh` を明示エスカレーション先とする構成へ改める。

`docs/routing.md` の "Coding escalation" は既に明示 override を `gpt-5.6-luna/xhigh → gpt-5.6-sol/xhigh` と定めており、失敗のたびに Luna 継続の期待残 token と Sol へ移る期待残 token を再計算する(固定 retry 回数なし)としている。本 change はこの記述を正として、routing 設定と RouteSnapshot をこの意図に一致させる。

## What Changes

- **MODIFIED:** 本番 coding route の候補構成を「第一候補 `gpt-5.6-luna/xhigh` + 明示エスカレーション `gpt-5.6-sol/xhigh`」に変更する。対象は production 用 `vsm.toml`(オーナー指定: `_cutover_20260720_fable_activation/production/vsm.toml` の routing/candidates、64-65 および 106-116 行目付近)。
- **MODIFIED:** `docs/routing.md`(luna → sol override の記述、60 行目付近)と routing 設定の整合を取る。設定と docs が同一のエスカレーション意図を表すことを不変条件とする。
- **ADDED:** RouteSnapshot 再発行手順を仕様化する。新 snapshot を `register → S3_STAR_APPROVED → OWNER_APPROVED → PUBLISHED` へ進め、旧 `PUBLISHED` snapshot を `superseded_by_approved_snapshot` 理由の human Event で `RETIRED` にしてから後継を `PUBLISHED` にする。
- **ADDED:** エスカレーション条件を `docs/routing.md` の現行記述を正として仕様化する(どの失敗・判定で Sol へ上げるか)。

## Non-Goals

- Bayesian routing の posterior 計算方式や candidate identity hash の変更。
- coding 以外の route(Interface 等)の候補構成変更。
- 新モデルの追加や effort 値(xhigh)の変更。
- 自動エスカレーションの人工的発火(自然発生した Escalation Trace のみ計測する現行方針を維持)。

## Affected Invariants

RouteSnapshot は承認制であり、同一 `route_key` で routable な snapshot は一つだけという不変条件を維持する。旧版の retirement と後継 publish を一操作へ黙ってまとめない。`RETIRED` は dispatcher の選択対象外である。

## Rollout

新 snapshot の承認・publish はオーナー承認事項。docs と設定の整合修正は本 change 内で完結させ、snapshot の切替は承認取得後に実施する。
