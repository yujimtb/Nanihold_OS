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

Web Run は `vsm submit` と同じ `[agents]` 設定で backend を解決する。claude-code / codex は
各 CLI を直接使い、litellm は `LITELLM_PROVIDER` と対応する API キーを使う。設定された
backend が不足している場合は Run を開始せず、日本語の設定エラーを API と Run 状態へ記録する。
決定論 fake を使う場合だけ、`NANIHOLD_USE_FAKE_LLM=1` または `[agents.roles]` の backend に
`fake` を明示する。未設定のプロバイダを fake に置き換えることはない。

## 操作

実行画面では進捗と処理ログが随時更新される。

- **指示する**: S5 または選択した Node へ `INSTRUCTION` Message を配送し、Run 内文脈を保って反映する。
- **組織図**: 役割、実際に使った backend/model、状態、現在の活動、指示元、予算消費を Event_Log から再構成して表示する。
- **Node 介入**: lifecycle 契約に従って休眠・再開・停止する。
- **Algedonic / 合議 / Human review**: 人間からの緊急信号、合議意見、レビュー回答を入力する。
- **停止**: 現在の処理を中止する。
- 完了後は Markdown の最終回答と JSON の処理ログを Run 単位でダウンロードできる。

## 対話コンソール

トップバーの「対話」では、Nanihold のリポジトリを作業ディレクトリとして Claude Code または
Codex と会話できる。チャットIDごとに履歴とCLIの `session_ref` を
`runs/web/chat/<chat_id>.jsonl` へ保存するため、Webアプリ再起動後も同じ対話を継続できる。
同一セッションへの送信は直列化され、応答待ちの間の多重送信は HTTP 409 になる。

各メッセージには次の開発ブリッジがある。

- **このメッセージをRunとして実行**: `POST /api/runs` に本文を `goal` として投入する。
- **実行中Runへ指示として送る**: 選択したRunの `POST /api/runs/{run_id}/instructions` に本文を届ける。

主要APIは以下のとおり。

| API | 用途 |
|---|---|
| `POST /api/chat` | `backend` (`claude-code` / `codex`)、任意の `model` と `workdir` でセッション作成 |
| `POST /api/chat/{chat_id}/messages` | `text` をAgentRuntimeへ送り、応答本文・tokens・latencyを返す |
| `GET /api/chat/{chat_id}` | メッセージ列、累計tokens、backend/model、session_refを返す |

Run の作成・詳細応答にはロール別の `runtimes`（`role` / `backend` / `model`）も含まれる。

CLI呼び出しのチャットタイムアウトは既定300秒で、`ChatManager(timeout_seconds=...)` から変更できる。
