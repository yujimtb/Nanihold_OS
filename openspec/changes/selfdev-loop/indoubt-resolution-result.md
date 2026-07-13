# in-doubt 効果裁定・脱出経路 実装結果

更新日: 2026-07-13

## 対応した運用ギャップ

クラッシュ後に `WORKSPACE_READY` の Proposal が複数の in-doubt effect と複数の `SUSPEND` pause を持つと、外部事実を人間が裁定できず、`abort` は cleanup / integrity 解決待ちで拒否され、`resume` も pause の一意指定手段がない状態だった。本変更で、exactly-once の再実行禁止を維持したまま、Human が裁定して通常 abort または force abort へ進める経路を追加した。

## 実装内容

### In-doubt 効果の一覧と裁定

- `GET /api/selfdev/proposals/{proposal_id}` の `in_doubt_effects` に、未終端の `tool_invoked` ごとに次を返す。
  - `effect_id`
  - `effect_kind`
  - `input_sha256`
  - `invoked_at`
  - `invocation_event_id`
- 既存の `POST .../human-decision` を拡張し、`decision=completed|failed` と `effect_id`、理由、`expected_state_version` を受け付ける。
- `completed` は `tool_completed` に `recovered=true`、Human の裁定理由、Human actor を記録する。
- `failed` は `tool_failed` に `disposition=human_decision`、裁定理由、Human actor を記録する。
- 追加の `selfdev_effect_decided` event に effect、入力hash、journal event、理由を固定する。
- `tool_completed` / `tool_failed` のどちらも effect の終端として recovery から除外し、同じ effect を再実行しない。
- 同じ effect に対応する recovery pause が複数あっても、全ての対象 effect の裁定後に全て解除する。起動のたびに同じ recovery pause を増殖させない。

### pause_id 指定 resume

`ProposalControlBody` に `pause_id`（`resume` 時のみ任意）を追加した。複数 pause で省略した場合は 409、指定した pause が一意でなければ 409 とし、SUSPEND / QUOTA_WAIT のどちらも指定対象を一意に解決する。

### force abort

- control action に `force_abort` を追加した。
- in-doubt effect が残る間は拒否する。
- 全ての effect 裁定後、cleanup を再実行せず、既存 artifact の参照を `selfdev_force_aborted` event に保存して `ABORTED` へ遷移する。
- 理由と保全 artifact は durable event に記録し、artifact の bytes は変更しない。
- 通常 `abort` は全 in-doubt 裁定後に従来どおり cleanup を試行し、完了すれば成功する。cleanup不能時だけ force abort を使用する。

### WebUI / Playwright

- 自己開発タブに in-doubt 効果の一覧、種別・入力hash・発生時刻、`completed` / `failed` ボタン、理由入力を追加した。
- 複数 pause の場合は `pause_id` 選択欄を表示する。
- `frontend` にブラウザ操作テスト基盤が存在しなかったため、`@playwright/test` を devDependency として追加し、`playwright.config.ts` と `npm run test:e2e` を導入した。
- `frontend/tests/selfdev-indoubt.spec.ts` で、自己開発タブを開き、in-doubt 効果を completed 裁定し、送信 payload と表示消失を確認するテストを追加した。

## テスト結果

全て Windows 側の本体ツリーを Docker の一時 `app` コンテナへ bind mountして実行した。WSL の `/home/user/projects/Nanihold_OS` は本体より古い別コミットで対象テストが存在しなかったため、WSL 側ツリーへコピーや更新は行っていない。

```text
docker compose run --rm -T \
  -v /mnt/d/userdata/docs/projects/Nanihold_OS:/workspace app \
  python -m pytest --basetemp=/tmp/pt
```

結果: **465 passed**, 1 warning（既存 FastAPI / httpx deprecation warning）

追加シナリオを含む selfdev 30件も全て passした。

```text
frontend> npm run build
結果: 成功（tsc -b + Vite production build）

frontend> npm run test:e2e
結果: 1 passed
```

## 変更ファイルの要点

- `vsm/selfdev/effects.py`: in-doubt metadata、Human completed/failed journal、終端判定
- `vsm/selfdev/recovery.py`: `tool_failed` を終端として扱う recovery 判定
- `vsm/selfdev/controller.py`: effect 裁定、複数 recovery pause 解除、force abort
- `vsm/selfdev/events.py`: 裁定 / force abort / journal 理由の strict schema
- `vsm/web/selfdev.py`, `vsm/web/selfdev_models.py`: REST projection と request contract
- `frontend/src/SelfDevView.tsx`, `frontend/src/api.ts`, `frontend/src/types.ts`: UI/API client
- `tests/unit/test_selfdev_recovery_quarantine.py`: 実データ fixture に相当する `WORKSPACE_READY` + in-doubt 2件の裁定、pause_id resume、通常 abort、cleanup不能 force abort
- `frontend/tests/selfdev-indoubt.spec.ts`: Playwright ブラウザ操作テスト

git commit は作成していない。
