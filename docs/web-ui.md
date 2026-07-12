# Web UI

ローカルのダッシュボードでは、日本語の JSON タスク投入、リアルタイムの処理過程、ライブ組織図、
Node 介入、追加指示、Algedonic、Human 合議参加、停止、履歴、最終回答を利用できる。

## 起動

```bash
docker compose up -d app
docker compose exec app uvicorn vsm.web.app:app --host 127.0.0.1 --port 8000 --reload

# 別ターミナル
cd frontend
npm run dev -- --host 127.0.0.1
```

起動後に `http://127.0.0.1:5173` を開く。バックエンド API は `http://127.0.0.1:8000`。
同時に実行できる Run は 1 件。

`.env` または `vsm.toml` にモデルを設定していない場合は、外部 API を呼ばないデモモデルで動作
する。実モデルを使う場合は `.env.example` を参考に `LITELLM_PROVIDER` と対応する API キーを
設定し、Compose を再起動する(設定手順は [setup.md](setup.md) を参照)。

## 操作

実行画面では進捗と処理ログが随時更新される。

- **指示する**: S5 または選択した Node へ `INSTRUCTION` Message を配送し、Run 内文脈を保って反映する。
- **組織図**: 役割、backend/model、状態、現在の活動、指示元、予算消費を Event_Log から再構成して表示する。
- **Node 介入**: lifecycle 契約に従って休眠・再開・停止する。
- **Algedonic / 合議 / Human review**: 人間からの緊急信号、合議意見、レビュー回答を入力する。
- **停止**: 現在の処理を中止する。
- 完了後は Markdown の最終回答と JSON の処理ログを Run 単位でダウンロードできる。
