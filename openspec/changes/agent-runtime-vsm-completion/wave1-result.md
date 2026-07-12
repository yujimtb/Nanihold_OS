# Wave 1 実装結果

## 完了範囲

`tasks.md` 1.1〜1.6 を実装した。Wave 2 以降の Budget、クォータ休眠・自動復帰、
Node 単位のセッション保持、context view、AI 調停、Algedonic、Consortium、API / WebUI
機能は実装していない。

## 変更ファイル

### AgentRuntime 基盤

- `vsm/agents/__init__.py`
- `vsm/agents/runtime.py`
- `vsm/agents/backends/__init__.py`
- `vsm/agents/backends/_common.py`
- `vsm/agents/backends/claude_code.py`
- `vsm/agents/backends/codex.py`
- `vsm/agents/backends/litellm_adapter.py`
- `vsm/agents/backends/fake.py`

### 設定・実行配線

- `vsm/config.py`
- `vsm/systems/base.py`
- `vsm/systems/s1_worker.py`
- `vsm/systems/s2_coordinator.py`
- `vsm/systems/s3_allocator.py`
- `vsm/systems/s3star_auditor.py`
- `vsm/systems/s4_scanner.py`
- `vsm/systems/s5_policy.py`
- `vsm/runtime/lifecycle.py`
- `vsm/eventlog/schema.py`
- `vsm/web/manager.py`

### テスト・文書

- `tests/unit/test_agent_backends.py`
- `tests/unit/test_agent_config.py`
- `tests/unit/test_subagent.py`
- `docs/setup.md`
- `openspec/changes/agent-runtime-vsm-completion/tasks.md`
- `openspec/changes/agent-runtime-vsm-completion/wave1-result.md`

## 実装要点

- Claude Code は JSON、Codex は JSONL を厳密に解析し、非 JSON、非ゼロ異常終了、
  非 quota のエラーイベントを `AgentRuntimeError` に正規化する。
- 両 CLI は stdin へプロンプトを書いた後、明示的に close / wait_closed する。
- quota は既知の終了コードまたは診断文言から検知し、例外ではなく
  `AgentResult.quota_exhausted` として返す。ISO 8601 時刻が含まれる場合は
  `quota_reset_at` も設定する。
- `Platform.create` はロールごとに別 runtime インスタンスを生成する。
  `S3_ALLOCATOR = ""` は runtime を持たず、決定論処理のままとする。
- `llm_invocation` / `llm_timeout` / `llm_error` はイベント名を維持し、
  新規 payload を付けて schema version 2 で発行する。イベント schema は version 1
  の replay を受理できる。

## 設計からの逸脱

- `load_config()` の既存の 2 要素戻り値を維持するため、`AgentsConfig` と
  `SessionConfig` は `RunConfig.agents` / `RunConfig.session` として公開した。
  設定内容と解決規則は design.md §2 に一致する。
- 大量の既存テスト向けに明示された移行経路として `llm_override` 引数を残し、
  Platform 内でロール別 `FakeAgentRuntime` / `LiteLLMRuntimeAdapter` に変換する。
  System / SubAgent 内部には旧 `_llm` 経路を残していない。
- 完了条件に記載された Windows `.venv-win` は、プロジェクト `AGENTS.md` の
  「Windows Python 禁止、Docker Compose app で実行」に従って使用していない。

## 検証結果

- `docker compose exec -T app python -m pytest -q`: **345 passed**
- `docker compose exec -T app vsm --help`: 正常終了
- 新規テストは process factory モックのみを使用し、ネットワークアクセスなし。

実行時、文書上の WSL パス `/home/user/projects/Nanihold_OS` が Windows 作業ツリーと
別コピーだったため、同じ変更を参照する `/mnt/d/userdata/docs/projects/Nanihold_OS` を
作業ディレクトリとして Docker Compose を実行した。

## 残課題

Wave 1 内の残課題はない。`quota_exhausted` を受けた Node の suspend / 自動 resume は
Wave 2、Run 内・同一 Node の `session_ref` 保存と自動再開は Wave 3 の担当範囲である。
