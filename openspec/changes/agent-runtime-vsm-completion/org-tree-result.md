# 組織ツリー実装結果

## 実装概要

- Run詳細ページで組織ツリーを常時表示する構成へ変更した。SVGパスで親子エッジを描画し、HTMLカードを同じステージ上に配置している。
- 親子IDから自前で階層レイアウトを計算する `frontend/src/topologyLayout.ts` を追加した。S5固定の手作業配置ではなく、任意の深さのu-VSM再帰構成・複数ルート・一時的な未接続ノードを表示できる。
- ホイールでズーム、空白ドラッグでパン、ズーム率の変更と表示リセットを実装した。ノード数が増えてもステージ内の座標系を維持する。
- ノードカードには役割、backend/model、状態、活動概要、トークン予算バーを表示する。RUNNING中の直近tool/LLM活動は `last_activity_at` を使ってパルス表示する。
- RunのEventSource更新に加え、組織投影を1.5秒間隔で購読する既存ポーリングを継続し、Run停止後は最後に取得した形を表示する。
- イベント投影に `recent_events` と `last_activity_at` を追加し、ノード詳細で直近イベント、時刻、actor、活動概要、消費量、指示元を確認できるようにした。
- イベントタイムラインはRun詳細の右側に残し、最終回答と添付ファイルはツリーの下に配置した。

## 操作一覧

選択したノードの詳細パネルから次の操作を行える。

- `SUSPENDED` ノードの再開
- ノードの休眠
- terminableなノードの停止
- 選択ノードへの追加指示の送信
- Algedonicの痛覚・好機の発信
- 待機中の合議体への人間意見の投稿
- pending human reviewへの回答

操作成功時はackを受けた内容をツリーへ即時反映し、非同期でEvent_Log投影を再取得して最終状態へ同期する。既存の `/instructions`、`/nodes/{node_id}/control`、`/algedonic`、`/consortium/{id}/statement`、`/human-review` APIを利用しており、新規npm依存は追加していない。

## 検証

- `frontend`: `npm ci` 後の `npm run build` 成功
- `git diff --check`: 成功（改行コードに関するGit警告のみ）
- Python pytest: 未完走。Windows側の `docker compose run` はDocker CLIがComposeのrun引数を解釈できず `unknown flag: --rm` で終了。作業worktreeを明示したWSL経路でも、指定のインストール＋pytestコマンドが120秒超でタイムアウトした。環境の修復やサービス操作は行っていない。

## 残課題

- Docker/WSLのCompose実行環境を復旧した後、指定コマンドでPython側の全pytestを再実行する。
- 実ブラウザで大規模なノード数・長いラベル・モバイル幅の表示を確認し、必要ならカードの高さとパン初期位置を調整する。
- ノードのイベント履歴をさらに詳細化する場合は、バックエンドのイベント要約項目を仕様として固定する。

## 変更範囲

- `frontend/src/App.tsx`
- `frontend/src/styles.css`
- `frontend/src/types.ts`
- `frontend/src/topologyLayout.ts`
- `vsm/web/topology.py`

`vsm/web/chat.py` と `vsm/gates/` は変更していない。コミットも作成していない。
