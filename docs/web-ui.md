# Web UI

ローカルのダッシュボードでは、日本語のタスク投入、添付ファイル、リアルタイムの処理過程、
追加指示による再実行、停止、履歴、最終回答と成果物のダウンロードを利用できる。

## 起動

```bash
docker compose up --build
```

起動後に `http://localhost:5173` を開く。バックエンド API は `http://localhost:8000`。
同時に実行できる Run は 1 件。

`.env` または `vsm.toml` にモデルを設定していない場合は、外部 API を呼ばないデモモデルで動作
する。実モデルを使う場合は `.env.example` を参考に `LITELLM_PROVIDER` と対応する API キーを
設定し、Compose を再起動する(設定手順は [setup.md](setup.md) を参照)。

## 操作

実行画面では進捗と処理ログが随時更新される。

- **指示する**: 現在の Generation を打ち切り、最新の追加指示を優先した新しい Generation を
  開始する。旧 Generation のログは「差し替え済み」として履歴に残る。
- **停止**: 現在の処理を中止する。
- 完了後は Markdown の最終回答と JSON の処理ログを Run 単位でダウンロードできる。
