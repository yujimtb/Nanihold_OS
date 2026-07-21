# Change Proposal: add-agent-comm-routing

**Version:** 1.1
**Date:** 2026-07-21
**Status:** Proposed(オーナー確定事項を反映した設計 — 仕様のみ、実装は別 change)
**Repository:** Nanihold_OS
**Type:** 新規 capability `agent-comm-routing` の設計起草
**Source:** オーナー要望(2026-07-21)「返信は自動生成ではなく、各エージェントに対して疎通する通知システムと返信システムにする」
**Owner decisions:** 2026-07-21 夕(LETHE decision sup:c8e91a37 / sup:b3f7d215)で宛先記法・通知配送基盤・命名の自動割当・Nagi 常設席を確定。

> 注: 本 change は仕様・設計のみであり、実装しない。以前は「宛先規約・配送形態・命名割り当て運用」をオーナー承認待ちの論点として提示していたが、2026-07-21 夕のオーナー確定事項により、これらは確定仕様として本文・spec・design.md に反映済み。実装は承認後に別 change で行う。

---

## Why

現状、Discord / Slack の着信は `lethe-channel-bridge` を経て全て LETHE に取り込まれるが、そこに「**誰宛か**」という概念がない。着信は観測として一様に蓄積され、特定のエージェント(Nanihold の Node / Pilot 実行主体)を名指しした連絡でも、その相手へ届ける経路がない。返信側も、承認済みカードをブリッジが配信する仕組み(`reply-approval@1` → send)はあるが、**エージェント自身が返信文を書いて投入する経路**が存在せず、返信は事実上オーナーか自動生成に依存する。

加えて、現状は「どのエージェントが何をやっているか」の可視化がない。実行主体は `node_id` / `pilot_id` の機械 id でしか追えず、Ledger / receipt / チャネル通知の上で人が読める帰属がない。

オーナー要望は明確で、「返信は自動生成しない。各エージェントに疎通する通知システムと、各エージェントが返信する返信システムを作る」。さらに 2026-07-21 夕の確定事項で、実行エージェントへは WorkItem dispatch 時に個名を自動割当し(タスクごとに新規付与・プールをローテーション)、その名を Ledger / receipt / 通知に刻んで可視化することが決まった。本 change はこの通知(inbound routing)・返信(outbound authoring)・**個名の自動割当レジストリ**・監査を設計として起草する。実装は本 change のスコープ外。

## What Changes

- **ADDED:** ACR-01 エージェント個名レジストリとローテーション自動割当 — WorkItem dispatch 時に実行エージェントへ個名を**自動割り当て**する台帳(タスクごとに新規付与・プールをローテーション)。名前 ↔ `node_id` / `pilot_id` の写像を持ち、割り当てた名を Ledger / receipt / チャネル通知に刻んで「どのエージェントが何をやっているか」を可視化する。名前は `Agent_name.csv` から、規模 ↔ モデル階級・言語 ↔ 系統・いいねフラグの規則で選定する。
- **ADDED:** ACR-02 通知(inbound routing)— チャネル着信のうちエージェント名を宛先とするものを配送する機構。宛先記法は文頭 `@名前` を主、リプライ/スレッドの宛先継承を補とし、プレフィックス文字は設定値(例: `AGENT_ADDRESS_PREFIX`、既定 `"@"`)から解決(ハードコード禁止)。配送は Nanihold Operational Ledger(実体 = personal-primary LETHE `:8080`、`space:personal-primary`)イベントを基盤とし、必要時 WorkItem 起票へ昇格する二段構え。どの規約にも合致しない着信は配送せず観測のみ。
- **ADDED:** ACR-03 返信(outbound authoring)— エージェント自身が返信文を書き、**書き手のエージェント名を帰属付きで** `reply-draft@1` として card-queue へ投入する。オーナー承認(`reply-approval@1`)を経てブリッジが配信する(既存経路を流用)。返信文の自動生成ジェネレータは作らない。
- **ADDED:** ACR-04 監査 — 通知の配送・返信の帰属・dispatch で割り当てた個名を Nanihold Operational Ledger / receipt で追跡可能にする。
- **ADDED:** ACR-06 Nagi S5 常設席 — Interface node(`node:owner-interface`)の割当名 `Nagi`(凪)は手動割当済みの予約名でローテーション対象外。S5 最上位として終了条件を持たない常設の席とし、WorkItem 受け入れ条件体系の外に置く。名前は席に属し、パイロット交代でも不変。
- **MODIFIED:** なし(既存 `lethe-channel-bridge` の card-queue / import 経路は流用し、その契約は変更しない)。

## Non-Goals

- 返信文の自動生成(承認レスの自動送信を含む)。エージェントが書き、オーナーが承認する。
- `lethe-channel-bridge` の import / card-queue / send 契約の変更。本 change はその consumer / producer に徹する。
- LETHE 側 projection・承認 UI の実装。
- エフォートレベルと命名の連動(命名と無関係・別管理)。

> 旧版で Non-Goal としていた「名前プールからの自動割り当て禁止 / 割り当てはオーナーの明示決定」は、2026-07-21 夕のオーナー確定事項により**撤回**した。個名の自動割当(ローテーション)は本 change の設計対象である(ACR-01)。ただし `Nagi` は手動割当の予約席として例外(ACR-06)。

## Affected Invariants

「インターフェースは複数、実体は一つ」を維持する。着信・返信のデータ実体は LETHE / Nanihold Operational Ledger に一本化され、通知配送・返信帰属・個名の割当はその上のメタデータとして表現する。返信の配信経路は既存の `reply-approval@1` → ブリッジ send を唯一の出口とし、二重の送信経路を作らない。

## Owner 確定事項(2026-07-21 夕 / sup:c8e91a37, sup:b3f7d215)

1. **宛先記法(ACR-02)**: 文頭 `@名前` を主、リプライ/スレッドの宛先継承を補。プレフィックス文字は設定値(例: `AGENT_ADDRESS_PREFIX`、既定 `"@"`)でハードコード禁止。どの規約にも合致しない着信は配送せず観測のみ。
2. **通知配送基盤(ACR-02)**: Nanihold Operational Ledger(実体 = personal-primary LETHE `:8080`、`space:personal-primary`)のイベントとして配送し、必要時 WorkItem 起票へ昇格する二段構え。
3. **命名の自動割当(ACR-01)**: WorkItem dispatch 時に実行エージェントへ自動割当。タスクごとに新規付与しプールをローテーション。規模 ↔ モデル階級(3=旗艦 / 2=中堅 / 1=軽量)、言語 ↔ 系統(Claude=日本語名 / GPT=英名 / その他=ラテン名)、いいね=0 は使用禁止、枯渇時は数字サフィックス。エフォートレベルは命名と無関係。
4. **Nagi S5 常設席(ACR-06)**: `Nagi`(凪)は手動割当済みの予約名でローテーション対象外。終了条件を持たない S5 常設席、WorkItem 受け入れ条件体系の外。名前は席に属し不変。
5. **返信 outbound(ACR-03)**: エージェント名帰属付き `reply-draft@1` → `reply-approval@1`(オーナー承認)→ ブリッジ配信。自動生成ジェネレータ禁止。

## Rollout

本 change は上記オーナー確定事項を反映した設計仕様である。実装は本 change のスコープ外とし、確定した宛先記法・配送基盤・命名割当・Nagi 席・返信経路を反映した実装 change を別途起票する。
