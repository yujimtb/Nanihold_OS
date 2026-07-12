# Web Run の fake 暗黙フォールバック撤去・fail-fast 完了報告

## 実装内容

- `vsm/web/manager.py` の Run 起動から、LiteLLM 設定が無い場合に全ロールを
  `FakeAgentRuntime` へ差し替える経路を削除した。
- Web は `vsm/runtime/lifecycle.py` の共有 backend resolver を使い、
  `RunConfig.agents` に従って `claude-code`、`codex`、`litellm`、
  `deterministic` を選択する。
- `NANIHOLD_USE_FAKE_LLM=1`（`true` / `yes` も明示値として受理）または
  `[agents.roles]` の `fake` 指定時だけ fake を使用する。未設定のプロバイダを
  fake に置き換えない。
- Web Run 作成前に共有 resolver で設定を検証する。未設定ロールがある場合は
  runtime generation を作らず、Run を `failed` / `設定エラー` として保存し、
  ロール名・不足設定・修正方法を日本語の `error` に記録する。POST 応答と
  `GET /api/runs/{run_id}` の両方で確認できる。
- Run 応答へロール別 `runtimes`（`role` / `backend` / `model`）を追加した。
  初期値は共有 resolver の選択結果、実行後は `agent_attached` と
  `llm_invocation` の Event_Log から実際の値へ更新する。topology の
  `backend` / `model` は従来どおり Event_Log の実値を projection する。
- `LiteLLMRuntimeAdapter` に解決済みモデルを保持させ、初期 topology / Run 応答
  でも LiteLLM の実モデルを表示できるようにした。
- `docs/setup.md`、`docs/web-ui.md`、Web UI の表示文言を、暗黙デモモデルが無い
  fail-fast 方針へ更新した。

## テスト

- 追加: 実 CLI backend 選択、明示 fake 環境変数、LiteLLM provider 未設定時の
  generation 未作成、Web API / Run 状態への日本語エラー。
- 変更直結テスト: `46 passed`。
- `runs/web/chat` のホスト実行履歴を除外した Docker 一時コピーで、既存の
  `tests/unit/test_chat.py` 以外を含む全体: `381 passed, 1 skipped`。
- 全体実行では既存の
  `tests/unit/test_chat.py::test_chat_session_two_turns_restore_and_reject_busy`
  が単独でも `busy` の期待値 409 に対し 200 となった。このテストは今回の
  Run/backend 変更と無関係で、TestClient 同一 client の別スレッド要求が
  ハンドラ到達前に直列化される環境差によるもの。チャット実装は変更していない。
- Windows PowerShell の Docker は `docker compose` サブコマンドを持たないため、
  実行時は同等の `docker-compose run --rm --no-deps -u root app ...` を使用した。
