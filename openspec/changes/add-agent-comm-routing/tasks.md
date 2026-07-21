# Tasks: add-agent-comm-routing

> 本 change は設計起草のみ。実装しない。以下はオーナー確定事項(2026-07-21 夕 / sup:c8e91a37, sup:b3f7d215)を反映した設計項目と、実装 change 起票に向けた準備項目。

## Track A. 設計提示(確定事項の反映)

- [x] A1 通知(inbound routing)と返信(outbound authoring)の二本柱を design.md に図示する
  - Spec: ACR-02, ACR-03
- [x] A2 宛先記法(文頭 `@名前` 主 + リプライ/スレッド継承 補、プレフィックスは設定値 `AGENT_ADDRESS_PREFIX`)を確定仕様として記述する
  - Spec: ACR-02
- [x] A3 通知配送基盤(Nanihold Operational Ledger = personal-primary LETHE `:8080` `space:personal-primary` イベント + 必要時 WorkItem 昇格の二段構え)を確定仕様として記述する
  - Spec: ACR-02
- [x] A4 命名の自動割当(dispatch 時ローテーション・規模↔階級・言語↔系統・いいね規則・枯渇時サフィックス・エフォート非連動)を確定仕様として記述する
  - Spec: ACR-01
- [x] A5 Nagi S5 常設席(予約名・ローテーション対象外・終了条件なし・WorkItem 受け入れ条件体系の外・パイロット交代でも不変)を記述する
  - Spec: ACR-06
- [x] A6 返信=メンション同等(bot 配信メッセージへの返信 → 帰属エージェント宛、スレッド継承より優先、帰属不能時は Nagi 集約)を記述する
  - Spec: ACR-02
- [x] A7 エージェント間通信(個名宛てアドレッシング・ACR-02 と同一配送基盤・Ledger 監査・オーナー閲覧既定・外部発信禁止)を記述する
  - Spec: ACR-07

## Track B. 実装前提の固め(オーナー確定済み)

- [x] B1 宛先記法の確定(文頭 `@名前` 主 + 継承 補、プレフィックス設定値) — sup:c8e91a37 / sup:b3f7d215
  - Spec: ACR-02
- [x] B2 通知配送基盤の確定(Operational Ledger 基盤 + WorkItem 昇格) — sup:c8e91a37 / sup:b3f7d215
  - Spec: ACR-02
- [x] B3 命名の自動割当運用の確定(ローテーション・規模/言語/いいね規則) — sup:c8e91a37 / sup:b3f7d215
  - Spec: ACR-01
- [x] B4 Nagi 予約席の確定 — sup:c8e91a37 / sup:b3f7d215
  - Spec: ACR-06

## Track C. 監査設計

- [x] C1 通知配送・返信帰属・割当帰属の receipt / Operational Ledger トレース設計を記述する
  - Spec: ACR-04

## Track D. 検収

- [ ] D1 `openspec validate add-agent-comm-routing --strict` を通す
- [ ] D2 確定事項を反映した実装 change を別途起票する(本 change は実装しない)

## 実装 WorkItem 分割案(参考 / 本 change では実装しない・6 件構成)

1. 個名レジストリ + ローテーション自動割当エンジン(ACR-01 / ACR-06)
2. inbound 宛先解決パーサ(ACR-02)— 文頭 `@名前`・**返信=メンション解決(bot 帰属→宛先、Nagi 集約)**・スレッド継承・`AGENT_ADDRESS_PREFIX` 設定読込
3. 通知配送基盤(ACR-02)— Operational Ledger イベント + WorkItem 昇格の二段
4. outbound authoring 経路(ACR-03)
5. 監査トレース(ACR-04)
6. エージェント間通信(ACR-07)— #3 の配送基盤の上に構築(#3 の後)
