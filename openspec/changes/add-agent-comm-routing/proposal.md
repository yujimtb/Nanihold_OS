# Change Proposal: add-agent-comm-routing

**Version:** 1.0
**Date:** 2026-07-21
**Status:** Proposed(オーナーレビュー用の設計提案)
**Repository:** Nanihold_OS
**Type:** 新規 capability `agent-comm-routing` の設計起草
**Source:** オーナー要望(2026-07-21)「返信は自動生成ではなく、各エージェントに対して疎通する通知システムと返信システムにする」

> 注: 本 change は仕様・設計のみであり、実装しない。設計上の分岐(宛先規約・配送形態・命名割当の運用)は **オーナー承認事項** として本文および design.md に明示し、オーナーレビューに供する。

---

## Why

現状、Discord / Slack の着信は `lethe-channel-bridge` を経て全て LETHE に取り込まれるが、そこに「**誰宛か**」という概念がない。着信は観測として一様に蓄積され、特定のエージェント(Nanihold の Node / Pilot 実行主体)を名指しした連絡でも、その相手へ届ける経路がない。返信側も、承認済みカードをブリッジが配信する仕組み(`reply-approval@1` → send)はあるが、**エージェント自身が返信文を書いて投入する経路**が存在せず、返信は事実上オーナーか自動生成に依存する。

オーナー要望は明確で、「返信は自動生成しない。各エージェントに疎通する通知システムと、各エージェントが返信する返信システムを作る」。本 change はこの二本柱(通知 = inbound routing、返信 = outbound authoring)と、それを支える**エージェント個名レジストリ**および**監査**を設計として起草する。実装は本 change のスコープ外で、承認後に別 change で行う。

## What Changes

- **ADDED:** ACR-01 エージェント個名レジストリ — 名前プール(`Agent_name.csv`)からエージェント(Node / Pilot 実行主体)へ個名を割り当てる台帳。名前 ↔ `node_id` / `pilot_id` の写像を持つ。割り当ては **オーナー承認事項**。
- **ADDED:** ACR-02 通知(inbound routing)— チャネル着信のうちエージェント名を宛先とするものを、当該エージェントへの通知として配送する機構。宛先規約(先頭「名前:」・メンション等)と配送先の形態(Ledger イベント / 実行中 Execution への注入 / 新規 WorkItem 起票)は選択肢比較の上で推奨を出し、確定は **オーナー承認事項**。
- **ADDED:** ACR-03 返信(outbound authoring)— エージェント自身が返信文を書き、**書き手のエージェント名を帰属付きで** `reply-draft@1` として card-queue へ投入する。オーナー承認(`reply-approval@1`)を経てブリッジが配信する(既存経路を流用)。返信文の自動生成ジェネレータは作らない。
- **ADDED:** ACR-04 監査 — 通知の配送と返信の帰属を Nanihold Ledger / receipt で追跡可能にする。
- **MODIFIED:** なし(既存 `lethe-channel-bridge` の card-queue / import 経路は流用し、その契約は変更しない)。

## Non-Goals

- 返信文の自動生成(承認レスの自動送信を含む)。エージェントが書き、オーナーが承認する。
- 名前プールからの自動割り当て。個名割り当てはオーナーの明示決定とする。
- `lethe-channel-bridge` の import / card-queue / send 契約の変更。本 change はその consumer / producer に徹する。
- LETHE 側 projection・承認 UI の実装。

## Affected Invariants

「インターフェースは複数、実体は一つ」を維持する。着信・返信のデータ実体は LETHE / Nanihold Ledger に一本化され、通知配送と返信帰属はその上のメタデータとして表現する。返信の配信経路は既存の `reply-approval@1` → ブリッジ send を唯一の出口とし、二重の送信経路を作らない。

## Owner Review 論点(承認事項)

1. **宛先規約**: 着信のどの表現をエージェント宛と解するか(先頭「名前:」/ メンション / エイリアス表記)。誤配送・取りこぼしのトレードオフを design.md で比較。
2. **配送形態の推奨**: 通知の配送先を Ledger イベント / 実行中 Execution への注入 / 新規 WorkItem 起票のいずれにするか。design.md で 3 案を比較し推奨を提示。
3. **命名割り当ての運用**: 名前プールからの割り当て手続き、いいねフラグの扱い、規模・カテゴリ・意味座標の使い方。

## Rollout

本 change はオーナーレビュー用の設計提案である。上記 3 論点の承認を得たのち、確定した宛先規約・配送形態・命名割り当て運用を反映して実装 change を起こす。
