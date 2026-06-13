# Phase 2: 営業開始と移設(7/13–8/31)

## Why

Phase 1 で「死なない」ランタイムができた。本 change は、それを収益に変える —
初売上の定常化(サブスク + 受託)、チャットボットの Nanihold への移設、そして
**収益 > トークン燃焼** の達成。移設手順は単なる引っ越しではなく、プロダクトの中核命題
「既存組織のファジィな吸収」の最初の実証であり、β営業資料に転用する。

## What Changes

**7月中旬**
- `persistent-service-node`(N-15): 終了しないサービス Node のライフサイクルと会計
- `lethe-query-facade`(N-16): network_scope を LETHE に限定した読取 facade

**7月下旬**
- `chatbot-migration`(移設 + B-3): 無停止6手順での移設と「既存サービス吸収」標準 Runbook

**8月**
- `sales-intake`(B-4): 商品定義(S5 ポリシー)と受注導線
- `subtask-reuse`(N-17): 過去サブタスクの索引と再利用提示
- `dynamic-differentiation`(N-18): 固定フロー廃止・動的分化のデフォルト化

## Impact

- Affected specs (new): 上記6 capability
- Affected code: Nanihold(サービス Node・facade・分化判断 S5/S3・索引)、Discord bot 拡張、
  既存チャットボットの移設、運用 Runbook
- 依存: N-15←N-4/N-8 / N-16←lethe-api-auth(L-1)/ sales-intake は human-review(N-10)に
  「商品定義外の受注」を追加
- 体制: 友人合流(院試後・8月目処)。L-7 で立ち上げ、セキュリティレビュー第1回(B-5)
- マイルストーン: **8/31 サブスク + 受託2件目入金で収益 > 燃焼**、動的分化デフォルト、友人 LETHE 自走
