# Phase 5: プロバイダー製品化(2027/1 〜 3)

## Why

**2027-03 の組織 OS プロバイダー提供開始は確定・対外制約**。本 change は、βで得た metering と
提供価値の段(Lv0〜Lv2)を、課金可能な製品に変える。価格設計・非エンジニア向け操作面・
self-host デプロイパッケージ・提供開始の4点を確定し、3月に「課金が発生する契約が1件以上」を
DoD とする。法人化は提供開始のブロッカーにしない(個人事業主のまま開始可能)。

## What Changes

**1月**
- `pricing-model`(§9.1): 原価ベースの価格設計(組織月額 + 従量のハイブリッド、seat 課金を主にしない)

**1〜2月(製品化チェックリスト)**
- `operator-ui`(N-26): 非エンジニア向け操作面(CLI/events.jsonl なしで日常運用)
- `deploy-package`: docker compose 一式 + セットアップ文書 + 運用ドキュメント3種

**3月**
- `provider-launch`(§9.3): βからの転換 + 招待制新規受付、SLA(ベストエフォート)定義、提供開始

## Impact

- Affected specs (new): 上記4 capability
- Affected code: Nanihold(operator UI)、デプロイ一式(docker compose)、ドキュメント
- 依存: pricing-model←metering(N-25)/ operator-ui←human-correction-loop(N-22)/
  provider-launch←beta-onboarding+consent-package
- 体制・事業タスク: B-5 セキュリティレビュー第2回(友人主導)、B-7 法人化の再判断
- マイルストーン: **2027/2 製品化チェックリスト完了 / 2027/3 提供開始(課金契約1件以上)**

## Non-Goals(post-launch の継続開発)

2027/3 必須からは除外し、提供開始後に継続する: FSX 数値最適化(REQ 14.1)、公共性測定(REQ 14.2)、
共有剰余配分(REQ 14.3)、テンポラル・インターフェース完全版、**Lv3 自動化の限定開放**。
(前提となる計測 N-1/N-25 は先行整備済み。)
