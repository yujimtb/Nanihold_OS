# Tasks: add-agent-comm-routing

> 本 change は設計起草のみ。実装しない。以下はオーナーレビューと承認後の実装 change 起票に向けた準備項目。

## Track A. 設計提示(オーナーレビュー)

- [x] A1 通知(inbound routing)と返信(outbound authoring)の二本柱を design.md に図示する
  - Spec: ACR-02, ACR-03
- [x] A2 宛先規約 3 案(先頭「名前:」/ メンション / エイリアス)を比較し推奨を提示する
  - Spec: ACR-02 / 論点 1
- [x] A3 配送形態 3 案(Ledger イベント / Execution 注入 / WorkItem 起票)を比較し推奨を提示する
  - Spec: ACR-02 / 論点 2
- [x] A4 命名割り当ての運用(いいねフラグ・カテゴリ/規模/意味座標の扱い)を提示する
  - Spec: ACR-01 / 論点 3

## Track B. オーナー承認事項の確定(承認待ち)

- [ ] B1 宛先規約の確定
  - Spec: ACR-02 / 受け入れ: オーナー承認
- [ ] B2 配送形態の確定
  - Spec: ACR-02 / 受け入れ: オーナー承認
- [ ] B3 命名割り当て手続きの確定と初期割り当て
  - Spec: ACR-01 / 受け入れ: オーナー承認

## Track C. 監査設計

- [x] C1 通知配送・返信帰属の receipt / Ledger トレース設計を記述する
  - Spec: ACR-04

## Track D. 検収

- [ ] D1 `openspec validate add-agent-comm-routing --strict` を通す
- [ ] D2 オーナーレビューで 3 論点(宛先規約・配送形態・命名割り当て)の承認を得る
- [ ] D3 承認内容を反映した実装 change を別途起票する(本 change は実装しない)
