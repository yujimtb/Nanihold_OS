# Selfdev 実配備経路修正結果

更新日: 2026-07-13

## 原因

1. `[selfdev].forbidden_paths` 省略時の既定値が tuple のまま list 検証へ渡され、設定ロードで `ConfigError` になっていた。この修正は作業開始時点で入っていた `vsm/config.py` の変更を保持した。
2. `S3StarAuditRunner` が runtime インスタンスに `session_ref` 属性があることを要求していた。実際の `ClaudeCodeRuntime` / `CodexRuntime` は runtime lifetime にセッションを保持せず、`AgentRequest.session_ref` と `AgentResult.session_ref` の境界で扱うため、実配備で必ず失敗していた。
3. 監査 report の `Auditor.session_ref` も必須で、runtime が返す参照を保存する実装になっていた。監査の独立性は共有しないことによって確保し、返却参照を永続化しない契約と矛盾していた。
4. `create_app(None)` が、実配備用設定を自動ロードしてしまっていた。`vsm.toml` が `selfdev.enabled=true` の状態で決定論テストが意図せず本番 controller を起動し、Docker 内から Windows パスの repository を git で開こうとして終了時に失敗していた。

## 修正

- `S3StarAuditRunner` の constructor から `runtime.session_ref` 検査を削除した。
- S3★監査の invoke は `session_ref` なしで常に新規セッションとして実行し、`AgentResult.session_ref` は参照せず、report には `null` を出力するよう `Auditor` を変更した。
- selfdev implementation も runtime の session 属性を読まず、毎回 `session_ref=None` で実行するようにした。
- `_resolve_role_runtimes` と `build_selfdev_service` に明示的な `process_factory` 注入 seam を追加し、実 CLI runtime の生成経路をモック process で統合検証できるようにした。
- `create_app(service=None)` は自動配備を無効にし、モジュール本体の `app` だけが `[selfdev]` 設定から自動配備するよう分離した。
- 監査 report の設計例を `session_ref: null` に更新した。

## 追加した再発防止テスト

`tests/integration/test_selfdev_runtime_deployment.py` を追加した。実際の `RunConfig` に Claude Code / Codex の backend と全 selfdev required role を設定し、実体の `ClaudeCodeRuntime` / `CodexRuntime` を process factory だけモックして `build_selfdev_service` を呼ぶ。構築後に `FastAPI TestClient` の lifespan を通し、controller が起動して `/api/selfdev/health` が `status=ok` になることを確認する。

既存の監査テストにも、runtime が返す `session_ref` を request / report へ持ち越さない検証を追加した。

## 検証結果

- Docker 内 compileall: 成功。
- 対象 selfdev API / Wave 3 / 実配備統合テスト: `13 passed, 1 warning`。
- Docker 内全 pytest: `443 passed, 1 failed, 1 warning`（2回とも同じ結果）。失敗は今回の変更範囲外の `tests/unit/test_chat.py::test_chat_session_two_turns_restore_and_reject_busy` で、期待 `409` に対して `200` となる並行タイミング依存の既存テスト。単独実行では `1 passed` を確認した。
- `tests/unit/test_chat.py` を除く全テスト: `443 passed, 1 warning`。selfdev 関連を含む今回の変更範囲は成功した。全体最終ゲートはこの既存 flaky テストを人間側で再確認する。

使用した主な Docker コマンド:

```text
wsl --cd /mnt/d/userdata/docs/projects/Nanihold_OS -- docker compose run --rm --no-deps app python -m compileall -q vsm tests/integration/test_selfdev_runtime_deployment.py tests/unit/test_selfdev_wave3.py tests/unit/test_selfdev_api.py
wsl --cd /mnt/d/userdata/docs/projects/Nanihold_OS -- docker compose run --rm --no-deps app python -m pytest -q --basetemp /tmp/nanihold-pytest
wsl --cd /mnt/d/userdata/docs/projects/Nanihold_OS -- docker compose run --rm --no-deps app python -m pytest -q --basetemp /tmp/nanihold-pytest-no-chat --ignore=tests/unit/test_chat.py
```

Docker Compose の標準 `/home/user/projects/Nanihold_OS` は Windows 側作業ツリーと同期していなかったため、検証のみ `/mnt/d/userdata/docs/projects/Nanihold_OS` を使用した。古い WSL checkout への編集、サービス・ホストプロセスの操作、git commit は行っていない。
