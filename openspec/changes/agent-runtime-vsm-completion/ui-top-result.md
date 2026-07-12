# UIトップ改修 実装結果

## 実装内容

- ランディング画面の主役を既存のSVG組織図へ変更した。
  - アクティブなRunを優先して表示し、なければ最新Runの保存済み最終状態を表示する。
  - Runがない場合は組織図プレースホルダと「Runを開始」導線を表示する。
  - 複数Runをトップのセレクタで切り替えられる。
- 既存のRun詳細、作成フォーム、Run履歴、対話タブへの導線を維持した。
- `/api/config` の旧LiteLLM単一モデル表示を廃止し、`describe_role_runtimes` による全ロールの実ランタイム一覧を返すようにした。
  - `claude-code`、`codex`、`deterministic`、`fake` をロール別に反映する。
  - 右上バッジは短縮要約、詳細はツールチップで表示する。
  - fakeを含む場合はバッジを警告色にして `fake` を明示する。
- Runセレクタで組織図を切り替えたとき、古いRunの投影・ズーム・パンを持ち越さないようにした。
- 新規npm依存は追加していない。

## 検証

- `npm ci`: 成功（既存の `frontend/package-lock.json` を使用）
- `npm run build`: 成功
- `git diff --check`: 成功（Gitの改行コード警告のみ）
- 指定Python検証:

  ```text
  docker compose run --rm --no-deps -u root app sh -c "python -m pip install -q -e '.[dev]' && python -m pytest -q"
  ```

  実行環境のDocker CLIが `--rm` を受け付けず、`unknown flag: --rm` で即時終了した。サービス操作や環境修復は行っていないため、Python pytest全緑は未確認である。

## 変更ファイル

- `frontend/src/App.tsx`
- `frontend/src/styles.css`
- `frontend/src/types.ts`
- `vsm/web/app.py`
- `openspec/changes/agent-runtime-vsm-completion/ui-top-result.md`

コミットは作成していない。
