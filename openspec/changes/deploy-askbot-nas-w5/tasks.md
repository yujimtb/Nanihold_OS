# Tasks: deploy-askbot-nas-w5

## Track A. 前提条件(本番配備ゲート)

- [ ] A1 RAM 増設完了(16GiB 以上)を本番配備の前提ゲートとして明記する
  - Spec: NAS-01 / 受け入れ: 16GiB 未満では本番配備を開始しない
- [ ] A2 RAM 増設完了の確認手順(実測メモリの検証)を定義する
  - Spec: NAS-01 / 受け入れ: 増設完了後に本番配備を許可

## Track B. メモリ設計(総予算 16GiB)

- [ ] B1 各モジュールに `deploy.resources.reservations.memory`(最低保証)を設定する
  - Spec: NAS-02 / 受け入れ: 最低保証の総和が 16GiB を超えない
- [ ] B2 各モジュールに `deploy.resources.limits.memory`(上限)を設定し上限内で動的調整する
  - Spec: NAS-02 / 受け入れ: reservations と limits の両設定

## Track C. 配備規約

- [ ] C1 `/volume1/docker/ask-bot/` 配下に配置し、イメージ・ボリューム・一時ファイルを `/volume1` 側に置く
  - Spec: NAS-03 / 受け入れ: 配置とルート FS 逼迫回避
- [ ] C2 永続データを named volume で保持する
  - Spec: NAS-03 / 受け入れ: named volume 保持
- [ ] C3 機密を `.env` + `${VAR:?required}` 方式で注入し未設定時に起動失敗させる
  - Spec: NAS-03 / 受け入れ: 必須未設定で起動失敗・焼き込み無し
- [ ] C4 コンテナに `read_only` + `cap_drop: ALL` + `no-new-privileges` を適用する
  - Spec: NAS-03 / 受け入れ: 最小権限の適用

## Track D. データ移送

- [ ] D1 tar 圧縮ストリームの SSH 直送で移送し、ルート FS 上に大きな一時ファイルを滞留させない
  - Spec: NAS-04 / 受け入れ: SSH 直送・一時ファイル滞留なし

## Track E. ポート競合回避

- [ ] E1 ask-bot の DB ポートを `127.0.0.1:5432` と競合しない別ポート/別バインドで公開する
  - Spec: NAS-05 / 受け入れ: 5432 と非競合

## Track F. 試験環境での先行挙動調査

- [ ] F1 試験環境で ask-bot の挙動(メモリ実消費・ポート・起動順・依存)を先行調査する
  - Spec: NAS-06 / 受け入れ: 試験環境での先行調査
- [ ] F2 試験調査の結果を本番配備の前提評価とメモリ初期値へ反映する
  - Spec: NAS-06 / NAS-02 / 受け入れ: 実測に基づく本番値確定

## Track G. 検証

- [ ] G1 `openspec validate deploy-askbot-nas-w5 --strict` を通す
