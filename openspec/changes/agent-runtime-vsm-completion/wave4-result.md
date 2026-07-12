# Wave 4 実装結果

## 実装範囲

tasks.md 4.1〜4.5 のコードと決定論テストを実装した。

- S2 の `request_coordination` ハンドラを AgentRuntime に接続し、`decision` と `reason` を
  必須とする `coordination_decided` を記録するようにした。`[coordination]
  ai_deliberation = false` では `coordination_requested` の記録だけを行う。
- `ChannelId.ALGEDONIC`、Human 発信を表す `ExternalRole.HUMAN`、`AlgedonicFacade` を追加した。
  Bus の allow-list は任意の System role / Human から S5 への直送だけを許可する。
- S5 が AgentRuntime で `suspend` / `consortium` / `escalate` を選択し、根拠付きの
  `algedonic_handled` を記録するようにした。`notify_human` 有効時は
  `algedonic_human_notification` も残す。
- `vsm/runtime/consortium.py` に、Node 参照参加者、Human statement 枠、ラウンド制発言、
  招集者による統合、反対意見要約、human timeout の `proceed` / `abort` を実装した。
- S5 任意招集、Algedonic 連動、人間発の公開招集メソッドを同じ Consortium 実装へ接続した。
- `[coordination]`、`[algedonic]`、`[consortium]` のみを `vsm/config.py` に追加した。
- Wave 4 の Event Log event type と、関連する architecture / implementation status を更新した。

## テスト

追加テストは `tests/unit/test_wave4_runtime.py` にまとめた。

- AI 調停の決定・理由、および AI 無効化
- Node / Human の Algedonic バイパス配送
- S5 の Algedonic 対応選択
- Consortium の複数ラウンド、context view hook、Human timeout `proceed`
- Human timeout `abort`
- Wave 4 設定3節のロード

指定された次の全テストコマンドを実行したが、pytest 起動前にホストの Docker CLI で失敗した。

```text
docker compose run --rm --no-deps app sh -c "python -m pip install -q -e . pytest pytest-asyncio && python -m pytest -q"
```

結果:

```text
WARNING: Error loading config file: open C:\Users\mitob\.docker\config.json: Access is denied.
unknown flag: --rm
```

`docker compose version` も `docker: unknown command: docker compose` となった。指示どおり WSL / Docker
サービスの再起動・停止・修復や、Windows 側 Python による代替テストは行っていない。このため、
**全テスト緑という完了条件は環境要因により未確認**である。静的確認として `git diff --check` は
成功している。

## 仕様からの逸脱

- WebUI / API は Wave 5 のため実装していない。人間発 Algedonic、Human Consortium statement、
  人間発招集は Platform の公開メソッドまで実装した。
- Consortium の AI 参加者 context は `ContextViewBuilder` を import せず、注入可能な
  `ContextViewHook` を使用する。hook 未注入時は当該 Consortium の直近発言を簡潔なイベント要約として
  AgentRuntime に渡す。
- AgentRuntime が割り当てられていない決定論 Node は、Node の責務と直近文脈から規則的な statement
  を生成する。AI 参加者は必ず各 Node の AgentRuntime を使う。

## 残課題

- Docker Compose が利用可能な環境で指定コマンドを再実行し、既存テストを含む全緑を確認する。
- Wave 5 で人間向け通知イベント、Human statement 投稿、外部招集の公開メソッドを API / WebUI に接続する。
- `consortium_waiting` の dashboard projection は Wave 5 で実装する。

## Wave 2 / Wave 3 マージ時の注意

- `vsm/config.py` では `RunConfig` と `load_config` の引数組み立てが競合しやすい。Wave 2 の
  `[budget]` / `[quota]` と Wave 3 の設定を残しつつ、本 Wave の `coordination` / `algedonic` /
  `consortium` フィールドと extract 処理を統合すること。
- `vsm/runtime/lifecycle.py` では `Platform.__init__` / `create` / `start_run` に context hook と
  human waiter を追加している。Wave 2 の QuotaMonitor、Wave 3 の ContextViewBuilder / session 配線と
  引数を落とさず統合すること。
- Wave 3 の `ContextViewBuilder` 到着後は、`ContextViewHook` に adapter を注入する。Consortium から
  `vsm.memory.builder` を直接 import する必要はない。
- Wave 2 の suspend 実装と `suspend_node_from_algedonic` が Node 状態更新箇所で重なる可能性がある。
  Quota と Algedonic が同じ正式な lifecycle 操作を共有するよう統合し、二重遷移は fail fast に保つこと。
- `NodeRunState` の Wave 2 / 3 追加フィールドには本 Wave から依存していない。
- `vsm/cli.py` の status / runs 表示、budget / quota 節には触れていない。
