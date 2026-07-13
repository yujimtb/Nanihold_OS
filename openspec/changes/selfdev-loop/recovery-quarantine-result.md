# selfdev recovery quarantine 修正結果

更新日: 2026-07-13

## 真因

`runs/selfdev/proposals/proposal-e591ebe225714b05a64207ff38ff1a8c/workspace.json` の hash 不一致は、前回fatal時の書きかけではなく、`workspace.py` の write-once 違反が原因だった。

実データの Event Log seq=31 は `workspace.json` に `49375148d05fca2552dee709ec795d15ef5c8f5e17559f0a9d3dea1536bc0cc0` を登録している。一方、現存ファイルの hash は `b863a8695011cbf3b69833d7fb93c3a6a4571ccd3d7eda196cb0a3f2e406eb5f` で、cleanup 後の `status=closed` になっている。`acquire()` / `snapshot()` / `finalize()` が、artifact hash 記録後も status 更新のため同じ `workspace.json` を再書込みしていた。

修正では `workspace.json` を create 時だけ書き込む immutable descriptor とし、lifecycle status を `workspace-state.json` へ分離した。既存の hash 検査と改竄検知は維持している。

なお、現在の実データ Event Log は seq=37 で終わり、`ABORTED` state event はまだ存在しない。cleanup の完了と artifact 記録まではあるが、Event Log 上の projection は `WORKSPACE_READY` である。この事実を隠さず、実物の `proposal.json` / `workspace.json` / 登録 hash を fixture 化した。

## 隔離方針と実装

- Event Log 自体の strict recovery は継続し、torn、seq/stream逆行、未知schemaなど store 全体の破損は `RecoveryError` として起動を拒否する。
- ProposalManifest または `artifact_created` 対象の不整合は `proposal_integrity_failed` を一度だけ記録する。
- terminal Proposal は `disposition=isolated` として projection から除外し、artifacts は読み取り検査以外で変更しない。
- active Proposal は `disposition=needs_human` として projection を `NEEDS_HUMAN` 相当にし、自動stepを停止する。
- health に `integrity_failed_count` と Proposalごとの failure detail を出す。黙って受け入れる経路はない。

## 検証

`tests/fixtures/selfdev_recovery/` に実データ由来の fixture を追加し、次をテスト化した。

- terminal の `workspace.json` hash 不一致: 起動継続、terminal projection 除外、隔離イベント記録。
- active の `workspace.json` hash 不一致: 起動継続、`NEEDS_HUMAN` 化、自動step停止、artifact不変。
- health の隔離件数・詳細公開。
- workspace lifecycle 全体で `workspace.json` bytes が変化せず、status は `workspace-state.json` に記録されること。

Docker Composeの最終全suiteは、WSL `/home/user/projects/Nanihold_OS` とWindows側作業ツリーが別内容を参照しており、既存appコンテナから今回追加したテストが見えないため、この環境では完遂できなかった。`docker compose exec app python -m pytest` の最終ゲートは人間側で、Windows側本体ツリーと同じcheckoutをマウントしたappに対して再実行する。

今回の実測では、Windows側ツリーを `/mnt/d/userdata/docs/projects/Nanihold_OS` としてマウントした一時 app に対し、fixture 3件、wave2 14件、wave3/API 19件、workspace/wave1 11件がすべて成功した。全pytestは300秒で79%まで進んだ時点でタイムアウトし、全件数・全緑は確認できていない。

なお、最初のタイムアウト後に残った一時コンテナを掃除する際、誤って既存 `nanihold_os-app-1` も停止・削除した。直ちに元の `/home/user/projects/Nanihold_OS` checkout から `docker compose up -d app` を実行して復元し、その後の `docker compose ps` で `app` が Up であることを確認した。これは本修正に不要な操作ミスであり、再発させない。
