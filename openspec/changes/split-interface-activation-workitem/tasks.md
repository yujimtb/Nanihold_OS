# Tasks: split-interface-activation-workitem

## Track A. 廃棄と既達認定

- [ ] A1 WorkItem `work:interface-effective-activity-start` を廃棄する(再投入しない)
  - Spec: WD-01 / 受け入れ: キューからの除去確認
- [ ] A2 既達条件(ReorientationAssessment 提示済み・owner 承認済み ACTIVE)を認定し、再実行対象から除外する
  - Spec: WD-02 / 受け入れ: 既達認定(オーナー承認事項)

## Track B. 棚卸しと分割案

- [ ] B1 残作業を棚卸しし、分割案の骨子(a: チャネルブリッジ検収 / b: 次期 WorkItem 起票 / c: 実装系タスク群)を提案として提示する
  - Spec: WD-05 / 受け入れ: 骨子レビュー
- [ ] B2 各候補 WorkItem を 12k トークン / 300 秒で完了可能な粒度へ細分する
  - Spec: WD-03 / 受け入れ: 粒度見積り

## Track C. 起票要件

- [ ] C1 各新規 WorkItem の acceptance criteria に「12k トークン / 300 秒で完了可能」を明記する
  - Spec: WD-03 / 受け入れ: acceptance criteria レビュー
- [ ] C2 budget 制限値 12k/300 秒を変更していないことを確認する
  - Spec: WD-04 / 受け入れ: 制限値不変確認

## Track D. 承認と検収

- [ ] D1 分割リストのオーナー承認を取得する(承認前は確定起票しない)
  - Spec: WD-05
- [ ] D2 承認後に小粒 WorkItem 群を起票する
  - Spec: WD-01, WD-02, WD-03
- [ ] D3 `openspec validate split-interface-activation-workitem --strict` を通す
