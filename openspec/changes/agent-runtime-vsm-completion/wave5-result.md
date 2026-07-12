# Wave 5 実装結果

## 実装範囲

`tasks.md` 5.1〜5.5 を実装した。

- `POST /api/runs` を `goal` / `constraints` / `budget` 上書きの JSON 契約へ統一した。
- 追加指示、Human Algedonic、Consortium statement、topology、budget API を追加した。
- Node の suspend / resume / terminate と Human review 応答の Web 介入 API を追加した。
- `vsm instruct <run_id> "<text>" [--node <id>]` を追加した。接続先はローカル専用の
  `http://127.0.0.1:8000` である。
- Human→任意 Node の `INSTRUCTION` channel を追加した。追加指示は
  `instruction_received` event と `Message` の両方として記録・配送し、System 共通の受信ループが
  対象 SubAgent に反映する。
- `vsm/web/topology.py` に Event_Log 専用 projection を実装した。Node 親子、役割、状態、
  backend/model、直近活動、directive/decision/instruction 由来、Node/Run Budget、Human 待機を
  ランタイムのメモリ状態に依存せず再構成する。
- React ダッシュボードへ日本語の「組織図」を追加した。RUNNING / IDLE / SUSPENDED / WAITING の
  色分け、活動、指示元、token 消費バー、Node 操作、追加指示、Algedonic、Consortium statement、
  Human review 応答を扱う。購読方式は 1.5 秒ポーリングである。
- README の標準起動手順を Docker Compose `app` + uvicorn + Vite に一本化し、
  `docs/implementation-status.md`、`ROADMAP.md`、`docs/cli.md`、`docs/web-ui.md` を更新した。

## 主な変更ファイル

- `vsm/web/app.py`, `vsm/web/manager.py`, `vsm/web/topology.py`
- `vsm/runtime/lifecycle.py`, `vsm/systems/base.py`
- `vsm/messaging/channels.py`, `vsm/eventlog/schema.py`
- `vsm/cli.py`
- `frontend/src/App.tsx`, `api.ts`, `types.ts`, `styles.css`
- `tests/unit/test_wave5_api.py`, `tests/unit/test_web.py`
- `README.md`, `docs/implementation-status.md`, `docs/cli.md`, `docs/web-ui.md`, `ROADMAP.md`

## 契約上の決定

- 旧 multipart `POST /api/runs` と添付投入経路は残さず、Wave 5 仕様の JSON 契約へ破壊的に統一した。
- topology と budget は現在 Generation の `events.jsonl` を Source of Truth とする。Web Run と内部
  runtime Run の ID 統合は別課題であり、互換レイヤは追加していない。
- Human review 応答は pending request の存在と未回答を検証して
  `human_review_responded` を記録する。要求元 Tool の Future を再開する待機プロトコルは現行
  HumanReviewFacade に存在しないため、ロードマップへ明記した。
- frontend の新規 npm 依存は追加していない。

## テスト・検証

- `npm run build`: **成功** (`tsc -b && vite build`, 1741 modules transformed)
- `git diff --check`: **成功**（改行コードの将来変換 warning のみ）
- 同一 Windows 作業ツリーを参照する WSL パス `/mnt/d/userdata/docs/projects/Nanihold_OS` で
  `python3 -m compileall -q vsm tests/unit/test_wave5_api.py`: **成功**
- FastAPI TestClient の決定論テスト、Event_Log projection テスト、CLI HTTP mock テスト、
  instruction event + Message 配送テストを `tests/unit/test_wave5_api.py` に追加した。

指定された Docker テスト:

```text
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 test -q tests/unit/test_web.py tests/unit/test_wave4_runtime.py
```

は次の環境要因で pytest 起動前に失敗した。

```text
service "app" is not running
```

ユーザー制約に従い、Docker Compose `app` の起動、WSL/Docker/Windows service の再起動、
プロセス停止、Windows Python による代替テストは行っていない。このため
`docker compose exec -T app python -m pytest -q` の全緑は **環境要因で未実行** である。

## 残課題

- 人間側で Docker Compose `app` を起動後、指定の全 pytest を再実行する。
- Web Run と内部 runtime Run の identity / Budget を単一 Run に統合する。
- Human review 応答を要求元の待機 Future に接続する。
