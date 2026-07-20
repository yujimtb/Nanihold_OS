# Tasks: update-coding-route-luna-first

## Track A. 設定変更

- [ ] A1 production 用 `vsm.toml` の routing/candidates を「第一候補 `gpt-5.6-luna/xhigh` + 明示エスカレーション `gpt-5.6-sol/xhigh`」へ修正する
  - Spec: CRT-01, CRT-02 / 対象: `_cutover_20260720_fable_activation/production/vsm.toml`(64-65, 106-116 行付近)
  - 受け入れ: 候補構成テスト
- [ ] A2 `docs/routing.md`(60 行付近)の luna → sol override 記述と設定の整合を確認・修正する
  - Spec: CRT-04 / 受け入れ: 設定と docs の一致テスト

## Track B. エスカレーション仕様

- [ ] B1 `docs/routing.md` "Coding escalation" を正として、エスカレーション条件(どの失敗・判定で Sol へ上げるか)を仕様化する
  - Spec: CRT-02, CRT-03 / 受け入れ: 条件記述レビュー
- [ ] B2 失敗ごとの期待残 token 再計算(固定 retry なし)と自然発生 Escalation Trace のみ計測を確認する
  - Spec: CRT-03, CRT-02 / 受け入れ: 期待残 token 再計算テスト

## Track C. RouteSnapshot 再発行(承認制)

- [ ] C1 後継 snapshot を register する(Luna 第一候補構成、candidate identity hash 再計算)
  - Spec: CRT-05 / 受け入れ: register 受理
- [ ] C2 S3* 承認 → owner 承認を取得する(オーナー承認事項)
  - Spec: CRT-05
- [ ] C3 旧 PUBLISHED を `superseded_by_approved_snapshot` の human Event で RETIRED にする
  - Spec: CRT-05 / 受け入れ: 単一 routable snapshot 不変条件テスト
- [ ] C4 後継を PUBLISHED にする(retirement と別 command)
  - Spec: CRT-05 / 受け入れ: publish 後の dispatcher 選択が Luna 第一候補

## Track D. 検収

- [ ] D1 `openspec validate update-coding-route-luna-first --strict` を通す
- [ ] D2 SHALL 被覆表を作成する
