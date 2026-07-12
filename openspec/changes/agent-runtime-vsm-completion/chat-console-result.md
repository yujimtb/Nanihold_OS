# Chat Console 実装結果

## 変更一覧

- `vsm/web/chat.py` に ChatSession 管理を追加した。
  - Claude Code / Codex の既存 AgentRuntime をセッションごとに保持する。
  - `AgentRequest.session_ref` に前回応答の参照を渡し、`AgentResult.session_ref` を次回へ保存する。
  - `runs/web/chat/<chat_id>.jsonl` にセッション作成・ユーザー/アシスタントメッセージを追記し、起動時に復元する。
  - 同一セッションのCLI呼び出し中は409、タイムアウト既定値は300秒。
- `vsm/web/app.py` にチャット作成、メッセージ送信、履歴取得APIを追加した。Run APIは既存実装を利用する。
- `frontend/src/App.tsx`、`api.ts`、`types.ts`、`styles.css` に日本語の「対話」タブ、送信中表示、累計tokens、backend/model表示、Runブリッジを追加した。Run詳細には「実行結果 / 組織図」タブを追加した。
- `tests/unit/test_chat.py` に TestClient + FakeAgentRuntime の2往復、session_ref継続、JSONL復元、実行中の409を追加した。
- `docs/web-ui.md` と `docs/implementation-status.md` を更新した。

## API仕様

### `POST /api/chat`

```json
{
  "backend": "claude-code",
  "model": "任意のモデル名",
  "workdir": "任意。省略時はリポジトリルート"
}
```

`backend` は `claude-code` または `codex`。レスポンスは `chat_id`、backend/model、workdir、空のメッセージ列、累計tokensを返す。

### `POST /api/chat/{chat_id}/messages`

```json
{"text": "Naniholdの実装を確認して"}
```

レスポンスには `text`、`tokens`（input + output）、`latency` / `latency_ms`、token内訳、更新後の `session_ref` を含む。履歴にはuser/assistantのメッセージを順番どおり保持する。

### `GET /api/chat/{chat_id}`

セッション情報、メッセージ列、累計tokens、再開用 `session_ref` を返す。未知のIDは404、空メッセージは422、処理中の同一セッションへの送信は409、タイムアウトは504となる。

## 検証結果

- `npm run build`: 成功（TypeScript build + Vite production build）。
- 指定コマンド `docker compose run --rm --no-deps -u root app sh -c "python -m pip install -q -e '.[dev]' && python -m pytest -q"` をWSLから実行した。
- 結果: **368 passed, 1 skipped**。

## 残課題

- チャットセッション一覧APIは未追加。UIはブラウザのlocalStorageに保持した直近chat_idを履歴APIで復元する。
- 認証は既存Web APIと同じくローカル利用前提で未実装。外部公開時は認証・作業ディレクトリの許可範囲・CLI権限の追加設計が必要。
