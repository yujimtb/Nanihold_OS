# Wave 3 / Wave 4 統合結果

## マージ結果

- `mission/wave3` を `main` へマージした: `7f809e5` (`Merge branch 'mission/wave3'`)
- `mission/wave4` を続けて `main` へマージした: `2e84b38` (`Merge branch 'mission/wave4'`)
- 上記2件だけをコミットした。以降の意味的統合、テスト追加、文書更新は未コミットの
  ワーキングツリー変更として残している。

## 意味的統合

- `vsm/config.py` は `[agents]` / `[session]`、`[budget]` / `[budget.roles]` / `[quota]`、
  `[coordination]` / `[algedonic]` / `[consortium]` の設定型、`RunConfig` フィールド、
  厳格な extract 処理を全て共存させた。
- `NodeRunState` は `budget` / `cost_consumed` と backend 別 `session_refs` を同時に保持する。
- `Platform.__init__` / `create` / `start_run` は QuotaMonitor、ContextViewBuilder、
  `ContextViewHook`、`HumanStatementWaiter` の配線を全て保持する。明示 hook が無い場合は
  Platform が所有する `ContextViewBuilder` を adapter として Consortium に注入する。
  `vsm.runtime.consortium` は memory builder を直接 import しない。
- `Platform.shutdown` は EventLogWriter 停止前に QuotaMonitor の timer を停止し、全 System を
  停止した後に Run 内キャッシュである `session_refs` を破棄する。
- Quota の suspend/resume と Algedonic の suspend が共有する `transition_node_status` を追加した。
  この操作は Node と `NodeRunState` の同一性・状態一致・許可遷移を検証してから両者を一度だけ
  更新する。二重 suspend は `SUSPENDED -> SUSPENDED` の不正遷移として fail fast する。
  QuotaMonitor が復帰 timer を所有する Node だけを quota 休眠として扱うため、Algedonic 休眠を
  quota 保留メッセージ経路へ誤分類しない。

## resume フォールバックと二重課金対策

`SubAgent.respond` の1回を「論理呼び出し」とし、その内部に backend の「物理呼び出し」を置く。
保存済み session の resume が `AgentRuntimeError` になった場合だけ、session 参照を破棄し、完全な
context view を付けた新規 session を最大1回再試行する。

- Budget の事前検査、`tool_invoked`、最終 `llm_invocation` / `llm_error` は論理呼び出し単位で1回。
- `budget_consumed` と `cost_consumed` の加算は、最終的に返った `AgentResult` に対して1回だけ。
- 失敗した resume 試行は利用量を表す `AgentResult` が存在しないため課金せず、途中の
  `llm_error` も発行しない。
- 新規 session 再試行も失敗した場合だけ、論理呼び出し全体の `llm_error` を1回発行して例外を伝播する。

これにより resume fallback の物理呼び出しは最大2回になり得るが、論理イベントと取得可能な
成功結果の課金は二重化しない。

## 追加テスト

`tests/unit/test_wave_merge_integration.py` に次を追加した。

1. resume 失敗から新規 session へ復旧した際、物理呼び出し2回に対し
   `tool_invoked` / `budget_consumed` / `llm_invocation` と消費累算が各1回であること。
2. Algedonic suspend 後の quota suspend、および quota suspend 後の Algedonic suspend が
   二重状態更新をせず fail fast し、双方の timer/event が干渉しないこと。
3. Platform 経由の Consortium AI 参加者が `ContextViewBuilder` の実際の日本語 context view を
   `AgentRequest.context_view` で受け取ること。

## 検証結果

2026-07-12 に指定順で試行したが、いずれも pytest 起動前に環境要因で終了した。

1. `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 test -q`
   - WSL の systemd user session 起動に失敗。
   - Compose の `app` サービスが起動していないため終了。
2. `docker compose exec -T app python -m pytest -q`
   - Windows 側 Docker CLI が Compose を認識せず、`-T` を不明な Docker 本体フラグとして拒否。
   - `C:\Users\mitob\.docker\config.json` もアクセス拒否。

制約に従い、WSL / Docker / Windows サービスの再起動、停止、修復は行っていない。
Windows Python も使用していない。静的検証として conflict marker が無いこと、必須設定・引数・
shutdown 順序・共有 lifecycle 呼び出しを確認し、`git diff --check` は成功した。

## 残課題

- WSL + Docker Compose の `app` サービスが正常な環境で
  `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 test -q` を実行し、
  既存テストを含む全 pytest の最終ゲートを通す必要がある。
- Wave 5 の API / WebUI 接続と dashboard projection は今回の統合範囲外である。

## 追補: budget_consumed 欠落修正

2026-07-12 の再検証で、
`test_resume_retry_records_one_logical_invocation_and_one_budget_charge` の
`budget_consumed` 欠落を修正した。テストの期待は design.md §3 と本書の
「resume フォールバックと二重課金対策」に一致しているため変更していない。

直接原因は resume 再試行や課金集約ではなく `EventLogWriter.stop()` にあった。
`Platform.after_agent_invoke()` は成功結果を `NodeRunState.cost_consumed` へ1回だけ累算し、
`budget_consumed` も1回だけ writer queue へ受理させていた。しかし shutdown が writer task を
即時 cancel していたため、呼び出し直後に shutdown すると queue 後尾の
`budget_consumed` 以降が永続化前に破棄され得た。

- `EventLogWriter.stop()` は FIFO queue に終端 sentinel を追加し、それ以前に受理したイベントを
  すべて書き切ってから writer task とファイルを閉じるよう変更した。
- 待機時間に依存せず、stop 直前に受理した複数イベントが順序どおり永続化される回帰テスト
  `test_writer_stop_drains_all_accepted_events` を追加した。
- これにより物理呼び出し2回の resume fallback でも、論理 `tool_invoked`、
  `budget_consumed`、`llm_invocation` は各1件となり、成功結果の課金も1回のままである。

### 再検証結果

文書記載の `/home/user/projects/Nanihold_OS` は現時点で Windows 作業ツリーと同期しておらず、
未コミット統合テストを含まない別実体だった。Windows 作業ツリーの WSL 側実体
`/mnt/d/userdata/docs/projects/Nanihold_OS` から、許可された Docker Compose 一時コンテナで
次を実行した。WSL / Docker サービス操作やプロセス kill は行っていない。

1. 対象テスト、writer 回帰テスト、`test_wave2_budget_quota.py`: `6 passed`
2. Wave 2/3、SubAgent、EventLog、統合テスト一式: `27 passed`
3. 全 pytest: `366 passed in 89.54s`

したがって、上記「検証結果」に記録した環境要因と「全 pytest の最終ゲート」は本追補の
再検証で解消済みである。
