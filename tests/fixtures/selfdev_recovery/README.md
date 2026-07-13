# selfdev recovery fixture

`proposal-e591ebe225714b05a64207ff38ff1a8c` の実運用データから、`proposal.json`、`workspace.json`、および `artifact_created` の登録 hash をコピーしたfixtureです。

実データの `workspace.json` は cleanup 後の `status=closed` で、seq=31 に記録された hash (`493751...`) と一致しません。元の Event Log は seq=37 で終わり、`ABORTED` state event はまだ存在しないため、テストではこの実物ファイルと登録 hash を使って terminal/active の両方を構成します。
