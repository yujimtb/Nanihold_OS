# Wave 2 実装結果

## 完了範囲

`tasks.md` 2.1〜2.6 を実装した。Wave 3 以降の context view、Node 内セッション再開、
AI 調停、Algedonic、Consortium、Budget Web API / WebUI は実装していない。

## 変更一覧

- `vsm/config.py`: `BudgetConfig` / `QuotaConfig` と `[budget]` / `[budget.roles]` /
  `[quota]` の厳格な TOML ロードを追加。
- `vsm/runtime/lifecycle.py`: Node/Authority への envelope 注入、Node/Run 消費累算、
  呼出前強制、`EscalationFacade` 経由の escalation、quota monitor 接続を追加。
- `vsm/systems/base.py`: SubAgent の呼出前・呼出後 runtime control hook を追加。
- `vsm/runtime/quota.py`: Node suspend、自動 resume、reset 不明時の設定間隔、timer 所有と
  shutdown cleanup を行う `QuotaMonitor` を追加。
- `vsm/messaging/bus.py`: suspended Node 別の重複排除付き保留 Message キューと復帰時再投入を追加。
- `vsm/systems/s1_worker.py`, `s4_scanner.py`, `s5_policy.py`: quota 検知時に処理中
  Message を完了扱いにせず再投入へ引き渡す経路を追加。
- `vsm/eventlog/schema.py`, `vsm/errors.py`: `budget_exceeded`, `quota_exhausted`,
  `quota_resumed` と専用例外を追加。
- `vsm/cli.py`: `status` の Node 別消費、`runs` の Run 合計トークン/時間表示を追加。
- `tests/unit/test_wave2_budget_quota.py`: 注入、3種トークン/時間累算、強制、escalation、
  AgentResult quota 連携、FakeClock 自動復帰、処理中/休眠中 Message 非消失、timer cleanup を検証。
- `tests/unit/test_cli_status.py`, `tests/unit/test_cli_runs.py`: Budget 表示を検証。
- `docs/setup.md`, `docs/cli.md`, `docs/architecture.md`, `docs/implementation-status.md`:
  設定、表示、実装済み runtime policy を更新。

## 実装上の決定

- token 上限は `tokens_in + tokens_out + tokens_cache_read` の合計で強制する。
- AgentRuntime 呼出後に上限を越えた場合、その結果は記録し、次回呼出前に拒否する。
  事前に応答 token 数を予測できないためである。
- ロール別 envelope が未指定の Node は Run envelope を Node envelope として使う。
- quota 応答も返却された利用量を `budget_consumed` に記録してから suspend する。
- Node の wall clock は AgentRuntime latency の累計と、RUNNING 開始からの経過時間を別キーで持つ。

## 設計からの逸脱

- design.md §3 にある Budget Web API は §10 / Wave 5 の担当であるため、本 Wave では実装していない。
- `weekly_fallback_resume_minutes` は設定・検証するが、現行 AgentResult には quota 種別がないため、
  reset 時刻不明時は `fallback_resume_minutes` を使用する。週次 fallback の選択は quota 種別が
  AgentResult に追加されるまで行えない。

## 検証結果

- Wave 2 + CLI 対象テスト: `17 passed`
- `.venv-win\Scripts\python.exe -m pytest -q`: **348 passed, 1 skipped**

## 残課題

Wave 2 内の既知残課題はない。Wave 5 の Budget API / WebUI は後続 Wave の担当である。
