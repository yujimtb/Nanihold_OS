# Phase 4: クローズドβ(10月 〜 12月)

## Why

外部2組織以上で「**観測 → 構造表示 → 提案**」まで到達し、2027/3 の価格設計に足る
metering データを得る。β開始は「**同意パッケージ × 観測層 MVP × マルチテナント**」の
AND 条件であり、どれか1つの遅延がβ全体を遅らせる。ベータ先(HUMAI / SHIMOKITA)は
個人情報感度が高く、同意パッケージは「あれば良い」ではなく「ないと話が始まらない」資料。

## What Changes

- `consent-package`(B-6): 同意・プライバシーパッケージ4点(明示オプトイン)
- `beta-onboarding`: リクルート選定・提供形態2択のオンボーディング・受け入れ上限3組織
- `multi-tenant-runtime`(N-24): org 単位の Event_Log/ParentAuthority/予算分離を実トラフィックで検証
- `metering`(N-25): org 単位の月次原価集計(価格設計の入力)
- `beta-operations`: 週次サイクル・提供価値の段付け(Lv0→Lv2)・社会的受容性の早期警報

## Impact

- Affected specs (new): 上記5 capability
- Affected code: Nanihold(マルチテナント実運用・metering・週次サイクル運用)、
  オンボーディング手順書、同意・協定文書
- 依存: N-24←N-12/L-4 / metering←N-1+N-24 / consent-package←L-7+L-8 /
  beta-operations←structure-inference(N-21)+human-correction-loop(N-22)+automation-levels(N-23)
- **Lv3 はβ期間中は開放しない**
- マイルストーン: **12/31 β成立**(稼働2組織以上・O5 参加70%・「実態に近い」過半・不快ゼロ・org 月次原価把握・収入継続)

## β成功指標(12/31 判定)

| 指標 | 目標 |
|---|---|
| 稼働組織数 | 2 以上(3ヶ月継続)|
| O5 修正への運営者参加 | 週次サイクルの 70% 以上 |
| 「実態に近い」評価 | 月3 時点で過半 |
| 不快・不安の申告 | 重大事案ゼロ(軽微は改善対象として歓迎)|
| org 月次原価 | 把握済み(価格設計に十分な精度)|
| 受託・サブスク収入 | 継続(βは収入を毀損しない)|
