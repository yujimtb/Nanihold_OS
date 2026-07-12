# Wave 4 実装結果 — API / CLI / WebUI / 運用配線

更新日: 2026-07-13

## 実装範囲

- `vsm/web/selfdev_models.py` に Proposal 作成、control、Human decision、merge outcome の strict transport model を追加した。未知フィールド、空の理由、`respond` の statement 欠落を受け付けない。
- `vsm/web/selfdev.py` に `/api/selfdev` 専用 router と projection を追加した。create/list/detail、SSE events、control、human-decision、merge-outcome、allow-list artifact、health を提供する。`state` / `pending_action=human` / `state=MERGE_READY` の一覧 filter、stale state version、protected Manifest/scope hash、terminal 操作、path traversal を入口で拒否する。
- `vsm/web/app.py` に FastAPI lifespan と `create_app(service=...)` を追加した。`SelfDevService` は single app worker 内で controller lock を取得し、fatal/degraded 時の mutation は 503 になる。`vsm/web/selfdev_runtime.py` は `[selfdev].enabled=true` と S1/S3/S4/S5/S3★ の明示 runtime が揃った場合だけ本番配線を構築する。
- `vsm/cli.py` に `vsm selfdev propose/list/show/approve/reject/respond/suspend/resume/abort/outcome` を追加した。mutation と observation は loopback REST のみを使い、API停止時に Event Log へ直接 fallback しない。
- frontend に「自己開発」タブを追加した。全件・承認待ち・MERGE_READY一覧、状態 rail、pause、Manifest、状態遷移、合議全文、gate/audit/budget、artifact、candidate、PR説明文 copy、Human decision、suspend/resume/abort、merge/archive outcome、新規Proposalフォームを表示する。push/PR作成/mergeボタンは持たない。
- `compose.yaml` の app を `--workers 1` に固定し、`--reload` を除去した。自己開発 controller の二重起動を避ける。
- `docs/cli.md`、`docs/web-ui.md`、`docs/self-development.md`、`docs/setup.md`、`docs/implementation-status.md`、`README.md` を Wave 4 契約へ更新した。

## API / CLI / UI 仕様

正規 endpoint は `/api/selfdev` のみである。`/approvals`、`/merge-ready` alias は追加していない。

| surface | 契約 |
|---|---|
| REST | `POST /api/selfdev/proposals`、`GET /api/selfdev/proposals`、`GET /api/selfdev/proposals/{id}`、`GET .../events`、`POST .../control`、`POST .../human-decision`、`POST .../merge-outcome`、`GET .../artifacts/{name}`、`GET /api/selfdev/health` |
| CLI | `vsm selfdev propose --file`、`list [--state] [--pending-action human] [--json]`、`show [--json]`、approve/reject/respond、suspend/resume/abort、`outcome --merged|--archived` |
| WebUI | 既存の日本語タブ・カード表現に合わせた Proposal 専用 projection。PR説明文 copy は `MERGE_READY` かつ説明文存在時だけ有効 |
| error | manifest/schema は 422、stale/illegal/terminal/競合は 409、Proposal/artifact 不在は 404、controller 未配備/fatal/durable failure は 503 |

## 逸脱・判断

- 現行の本番設定は `SelfDevConfig.enabled` が既定 `false` なので、設定未投入時は selfdev health が `degraded/unconfigured`、mutation が 503 になる。暗黙の fake runtime や local direct store fallback は追加していない。pilot 前に `[selfdev]` と全 required runtime を明示する必要がある。
- API の actor 認証基盤は既存 Web に存在しないため、ローカル loopback の Human actor として `web-api` を記録する。公開 bind は行わず、Compose の利用範囲を loopback に限定する運用前提を docs に追記した。
- `MERGE_READY` の Human 結果は既存 controller の `record_merge_outcome` に委譲し、push/PR作成/merge 自体は実装していない。
- production Gate worker は既存 trusted `run_v2` を `asyncio.to_thread` で呼ぶ adapter とした。candidate worktree から GateRunner をロードする経路はない。
- `pending_action=human` は `NEEDS_HUMAN` に限定した。quota resume は controller の quota lifecycle に委譲し、UI/CLI の `resume` は pause cause が一意な場合のみ受理する。

## テスト・確認

- 新規テスト: `tests/unit/test_selfdev_api.py`（FastAPI TestClient、FakeAgentRuntime を含む deterministic controller seam）、`tests/unit/test_selfdev_cli.py`（Typer CliRunner）を追加した。
- `python -m compileall -q vsm tests/unit/test_selfdev_api.py tests/unit/test_selfdev_cli.py`: 成功（既存 image 内）。
- 新規 API 4シナリオ（create/list/detail/health、stale version/artifact traversal、SSE、503/422）を pytest 無しの直接実行で確認: 成功。
- `python -m vsm --help` / `python -m vsm selfdev --help`: 成功（既存 image 内）。
- `npm ci --offline`: lockfile にある既存依存のみ導入、package.json 変更なし。
- `npm run build`: 成功（TypeScript build + Vite production build）。
- `git diff --check`: 成功。
- 指定 pytest one-shot `docker compose run --rm --no-deps -u root app sh -c "python -m pip install -q -e '.[dev]' && python -m pytest -q"` は、WSL Docker Compose で 126 秒無出力 timeout し、pytest 開始結果を取得できなかった。代替の既存 image は pytest 未導入で、network 無効の pip install は `setuptools>=68` を解決できなかった。Docker/WSL の修復・サービス操作は行っていない。したがって全 pytest の最終ゲートは人間側で再実行する。

## Pilot 実施前チェックリスト

- [ ] `vsm.toml` の `[selfdev] enabled = true`、`repository`、`main` ref、worktree root の書込権限を確認する。
- [ ] S1_WORKER / S3_ALLOCATOR / S4_SCANNER / S5_POLICY / S3STAR_AUDITOR の backend・model・effort を明示し、Fake はテスト時だけ明示する。
- [ ] `docker compose config`、app の single worker、loopback bind、`GET /api/selfdev/health` が `status=ok` になることを確認する。
- [ ] `docker compose exec app vsm selfdev --help` と全 subcommand help、FastAPI TestClient の Wave 4 tests、全 pytest を実行する。
- [ ] frontend で `npm run build` を再実行し、ブラウザから全件・承認待ち・MERGE_READY・PR copy・artifact download を確認する。
- [ ] pilot 3件（docs WSL一本化、Compose loopback限定、CI拡張 protected）を順番に投入し、同時 active Proposal が1件だけであることを確認する。
- [ ] protected Proposal の事前 Human approval hash、scope conflict、quota reserve、依存 DONE 条件を開始前に記録する。
- [ ] `MERGE_READY` 到達後に push / PR作成 / merge が自動呼出しされていないことを Event Log と Git で確認する。
- [ ] 人間が candidate branch、`gate_report.json`、`audit_report`、PR説明文、budget actual を確認してから、外部 merge outcome を記録する。
