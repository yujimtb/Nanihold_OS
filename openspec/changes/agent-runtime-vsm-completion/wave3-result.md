# Wave 3 実装結果

## 完了範囲

`tasks.md` 3.1〜3.4 を実装した。Wave 2 の Budget、クォータ休眠・自動復帰、
`budget_consumed`、`vsm/runtime/quota.py` には依存せず、先回り実装もしていない。

## 変更一覧

### Context view と TaskSummary

- `vsm/memory/builder.py`
  - Node の直近イベント、親 Node の最新 directive、直接 child の TaskSummary、参照 Artifact を
    固定テンプレートの短い日本語ビューへ決定論的に構築する `ContextViewBuilder` を追加。
  - Node 不在、Run 外 Artifact 参照、Artifact 不在は暗黙に補完せず明示エラーとする。
- `vsm/tools/search.py`
  - Run と Node 集合を指定して TaskSummary を決定順で取得する `list_for_nodes` を追加。
- `vsm/systems/s1_worker.py`
  - S1 完了時に応答先頭行、assignment、疑問行、成功/失敗から規則ベースの `TaskSummary` を生成。
  - `runs/<run_id>/memory/task-summaries.jsonl` の `TaskSummaryIndex` へ登録し、Node の
    `summary_refs` と `summary_generated` event に反映。
  - prompt を「役割 + 今回の指示」に限定し、履歴は SubAgent が渡す context view に分離。

### Run 内セッション再開

- `vsm/nodes/model.py`
  - `NodeRunState.session_refs: dict[str, str]` を追加。キーは backend 名。
- `vsm/systems/base.py`
  - Platform に接続された SubAgent は初回呼び出しで完全 context view を渡す。
  - 同一 Run・Node・backend の次回呼び出しは保存済み `session_ref` を渡し、context view を省略。
  - resume 呼び出しが `AgentRuntimeError` になった場合、参照を破棄し、新規セッション + 完全
    context view で1回だけ再試行。再試行も失敗した場合は通常の `llm_error` として伝播する。
  - 成功結果の `session_ref` を `NodeRunState` へ更新。
- `vsm/runtime/lifecycle.py`
  - Node 接続時に Run/Node 状態と `ContextViewBuilder` を SubAgent へ配線。
  - Run shutdown 時に全 `session_refs` を破棄。

### テスト・文書

- `tests/unit/test_wave3_token_reduction.py`
  - context view の決定論、通常 resume、resume 失敗時の完全ビュー再送、S1 完了時の
    TaskSummary 登録を検証。
- `docs/setup.md`, `docs/implementation-status.md`, `tasks.md` を実装状態へ更新。

## 設計からの逸脱

機能契約からの逸脱はない。TaskSummary index の保存先は既存 Run ディレクトリ構造に合わせて
`memory/task-summaries.jsonl` とした。resume 失敗の backend 固有エラーコードは Wave 1 の共通契約に
定義されていないため、「session_ref を渡した呼び出しの `AgentRuntimeError`」を resume 失敗として
扱い、新規呼び出しを1回だけ試す。新規呼び出しも失敗すれば例外を隠さず伝播する。

## 検証結果

指定コマンド:

```text
docker compose run --rm --no-deps app sh -c "python -m pip install -q -e . pytest pytest-asyncio && python -m pytest -q"
```

は pytest 起動前に環境要因で失敗した。Docker image の `/opt/venv` は `vscode` 所有だが、
Dockerfile が `USER vscode` へ切り替える前に root で editable install するため、
`site-packages/__pycache__` と既存 `.pyc` が root 所有になっている。実行時エラーは
`Permission denied: /opt/venv/lib/python3.11/site-packages/__pycache__/__editable_...pyc`。
さらに image には既存 property test が必要とする `hypothesis` がなく、指定コマンドも
`hypothesis` または `.[dev]` をインストールしない。

プロジェクトファイルや常駐サービスを変更せず、一時コンテナだけで上記2点を補った次の検証を実施した。

```text
docker compose run --rm --no-deps --user root app sh -c "python -m pip install -q -e . pytest pytest-asyncio hypothesis && python -m pytest -q"
```

結果: **346 passed, 1 skipped**。

Wave 3 関連の限定検証は **10 passed**。

## 残課題

Wave 3 のコード上の残課題はない。完了条件の指定コマンドをそのまま通すには、Wave 3 外の開発環境修正として
Dockerfile の最終 install 後に `/opt/venv` の所有者を戻し、テスト依存を `.[dev]` から導入する必要がある。

## Wave 2 とのマージ注意点

- 競合候補は `vsm/nodes/model.py`, `vsm/systems/base.py`, `vsm/runtime/lifecycle.py`。
- `NodeRunState` では Wave 2 の `budget` / `cost_consumed` と、本 Wave の `session_refs` を両方残す。
- `SubAgent.respond` では本 Wave の resume fallback の内側または成功後に Wave 2 の事前 Budget 検査・
  消費記録を統合する。resume 失敗後の再試行を二重に課金・二重イベント化しないよう、物理 backend 呼び出し
  回数と論理 SubAgent 呼び出し回数の扱いを明示する。
- `Platform.shutdown` では Wave 2 の QuotaMonitor 停止・保留キュー処理と、本 Wave の session_refs 破棄を
  どちらも EventLogWriter 停止前に行う。
- `vsm/cli.py` の status/runs 表示、`vsm/config.py` の budget/quota 節、`vsm/runtime/quota.py` には触れていない。
