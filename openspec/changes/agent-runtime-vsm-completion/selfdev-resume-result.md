# selfdev-resume / quota circuit breaker 実装結果

## 実装内容

- `AgentResult.quota_kind` を `five_hour` / `weekly` / `unknown` の閉じた値として追加した。Claude Code / Codex の診断文字列は `_common.py` で `weekly`、`week`、`5-hour` 等を判別し、`quota_exhausted` に種別と `reset_at` を記録する。
- Quota の遮断単位を Node から認証 pool に変更した。標準 pool は `claude-subscription` と `codex-pro` で、同じ pool の全 Node を `QUOTA_WAIT` に遷移させ、`quota_pool_opened` を一度だけ記録する。復帰時は該当 runtime の health probe を一回だけ実行し、成功した場合に `quota_pool_closed` の後、Node を ID 順に逐次 resume する。probe 失敗時は quota 種別に応じた fallback interval で再試行する。
- Run ごとの `quota-state.json` に開いている pool、種別、`reset_at`、保留 Node、保留 Message を保存した。`Platform.create(..., resume=True)` は既存 Event_Log の static Node ID を再利用し、Platform 起動時に QuotaMonitor が状態と Message を復元して timer/probe を再構築する。Event_Log Writer も既存 seq/stream version の続きから追記する。
- `QUOTA_WAIT` 中は Node の running monotonic accounting を停止し、待機時間を budget wall-clock に加算しない。Web Run の 30 分期限は calendar deadline として保持し、quota 待機中は active timeout を停止する。calendar deadline を超過した場合は `web_timeout_deferred_quota` を人間向けに記録する。
- Claude Code、Codex runtime、Codex tool の timeout/cancel は独立 process group/session を作成し、POSIX は `killpg`、Windows は `taskkill /T /F` で子孫を含めて終了確認する。

## 復元手順

1. 実行中の `Platform` が quota 枯渇を検知すると `run_dir/quota-state.json` と Event_Log を同期更新する。Message が待機中に追加された場合も同ファイルを更新する。
2. プロセスを再起動したら同じ `run_id`、`runs_dir`、`RunConfig`、runtime 設定で `Platform.create(..., resume=True)` を呼ぶ。
3. `Platform.start()` を呼ぶと、復元された Node は `QUOTA_WAIT` のまま receiver を閉じ、`reset_at` 到達後の単一 probe を待つ。probe 成功後に保留 Message が各 Node の queue へ再投入される。
4. probe が失敗した場合はディスク上の pool 状態を更新し、次の fallback interval まで同じ pool を休眠させる。プロセスが再度落ちても disk state が優先される。

## テスト

- `tests/unit/test_selfdev_resume.py` に、kind 判別、pool 一括停止→一回の probe→逐次復帰、保留 Message、Platform 再作成による disk 復元、`QUOTA_WAIT` wall-clock 除外、process-group kill の決定論テストを追加した。
- WSL 側 Docker Compose の `app` サービス内で、最終変更反映後の指定コマンドを実行し、`406 passed, 1 skipped, 1 warning` を確認した。追加テスト単独でも `4 passed` だった。

```text
docker compose run --rm --no-deps -u root app sh -c "python -m pip install -q -e '.[dev]' && python -m pytest -q"
```

## 残課題

- quota health probe の実運用 prompt/CLI 固有の軽量 endpoint は backend ごとの CLI 仕様が確定した段階でさらに短縮できる。現在は既存 AgentRuntime 契約を使った一回の最小呼び出しである。
- Web Run 自体のアプリ再起動後の自動再接続は既存 RunManager の復旧方針（中断を failed とする）と別論点であり、今回の Platform-level durable resume の範囲には含めていない。
