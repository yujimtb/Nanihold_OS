# Phase 1: 生存基盤(6/25–7/12)

## Why

Phase 0 のチャットボットが収入源になった一方、Nanihold ランタイムはまだ
**「殺しても復旧し、浪費させても止まり、死にかけたら叫ぶ」** 状態にない。本 change は
Phase 1 のゴール — 自律実行を任せても事故が構造的に防がれるランタイム基盤と、受託で
商売をするための最小の配管 — を確定する。7/12 の障害演習6シナリオ通過をもって
「営業可能状態」とする。

## What Changes

**Week A(6/25–7/1): 死なない基盤**
- `web-ui`: Run 一覧 / LiveTopology / イベントストリームの観測面(初日にマージ、LAN 内のみ)
- `cost-accounting`(N-1): invocation 単位の実コスト会計(円)
- `budget-cap`(N-2): 二段判定のハードキャップ(自動引き上げ経路なし)
- `kill-switch`(N-3): 3経路からの全停止
- `runtime-resilience`(N-4): 常駐・replay 復旧・stale 回収・外部死活監視
- `algedonic-alerts`(N-5): WARN/ALERT/PAGE の階層バイパス警報

**Week B(7/2–7/8): 自律の最小骨格**
- `scheduler`(N-6): 冪等な発火と catch-up ポリシー
- `survival-report`(N-7): 日次生存レポート
- `watchdog`(N-8): ハートビート監視と自動 suspend
- `lifecycle-facades`(N-9): terminate/suspend/resume(ParentAuthority 検証)
- `human-review`(N-10): Discord 承認フロー
- `input-isolation`(N-11): 顧客入力隔離 + scope→sandbox 変換テスト

**Week C(7/9–7/12): 商売の配管**
- `org-customer-schema`(N-12): EventEnvelope への org/customer/provenance 追加
- `engagement-lifecycle`(N-13/N-14): 受託ライフサイクルイベントと請求・入金記録
- `model-tiering`(N-19): 役割別モデル階層(前倒し)

## Impact

- Affected specs (new): 上記15 capability
- Affected code: Nanihold ランタイム(イベント発行・projection・facade・CLI・Web UI)、
  systemd unit、Discord adapter、LiteLLM 連携
- 依存(順序を動かさない): **N-12 はすべてのテナント系機能の祖先**(後送りで replay 互換負債が膨らむ)。
  N-2←N-1 / N-7←N-1+N-6 / N-8←N-9
- マイルストーン: 7/1 死なない基盤 / 7/8 自律最小骨格 / **7/12 障害演習6シナリオ通過=営業可能状態**
