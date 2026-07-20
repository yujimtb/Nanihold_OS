# One-time migration

既存成果物は runtime の入力ではありません。一回限り command だけが source を読み、対象を personal/company DataSpace と Interface Node へ明示的に割り当てます。

対象:

- conversation message
- decision
- commitment
- Node memory

実行状態、会計状態、古い lifecycle は移しません。

## 1. Scan

```powershell
.\codex-dev.cmd compose run --rm app vsm migration scan --source /workspace/runs --summary
```

全ファイルの相対 path、byte 数、SHA-256、対象 event、所有先未確定 source を列挙します。

## 2. Ownership assignment

assignment file は各 source に次を一意に指定します。

- target DataSpace
- Node
- Interface Node
- Conversation

一つでも不明、重複、対象 DataSpace と不一致なら停止します。個人会話を company Lake へ暗黙複製しません。

## 3. Dry-run

```powershell
vsm migration dry-run \
  --source /workspace/runs \
  --assignment /workspace/migration-assignment.json \
  --output /workspace/migration-plan.json
```

plan は source manifest digest、import 件数、kind 別件数、ownership assignment を固定します。既存 output の上書きは拒否します。

Intercom、旧Nanihold、現況snapshotは、LETHEの一回限りimportへ渡す前に
`HistoryRawRecord` JSONLへ変換する。producerはruntimeから参照されず、出力先の
上書きもしない。

```powershell
python -m tools.history_source_export intercom `
  --export-dir C:\cutover\intercom `
  --require-cutover-ready `
  --output C:\cutover\history\intercom.jsonl `
  --report C:\cutover\history\intercom-report.json

python -m tools.history_source_export nanihold-legacy `
  --source-root C:\cutover\runs `
  --assignment C:\cutover\ownership.json `
  --output C:\cutover\history\nanihold-legacy.jsonl `
  --report C:\cutover\history\nanihold-legacy-report.json

python -m tools.history_source_export system-snapshot `
  --snapshot C:\cutover\system-snapshot.json `
  --output C:\cutover\history\system-snapshot.jsonl `
  --report C:\cutover\history\system-snapshot-report.json
```

Intercomはmanifestの件数・digest・drain完了を検証する。Intercomのsource-native
identityは`(stream, source_native_id)`であり、platform IDが同じでもinboxとoutboxなど
異なるstreamのrecordは別の履歴として保持する。同じstream内でnative IDとraw digestが
ともに一致する場合だけ同一recordとして扱い、それ以外のidentity衝突は停止する。
旧Naniholdは全sourceのownershipが過不足なく割り当てられるまで停止する。system snapshotは
`captured_at`、`source_instance_id`、`states`だけのsecret-free JSONを要求する。
各stateは`state_key`、表示用`text`、構造化`value`から成る。本文が同じ短文でも
source-native IDが異なるrecordは保持し、ID衝突は停止する。

現況snapshot自体は、cutover時刻を固定した明示specから作る。repositoryはHEAD、
branch、作業ツリー差分を、HTTP endpointは`selected_fields`だけを、設定ファイルは
本文ではなくbytes・digestだけを保存する。Bearer値は環境変数から読み、snapshotへ
書かない。WorkItem、Pilot、quota、activation、service healthはそれぞれのread-only
endpointをspecに列挙する。endpoint失敗やselected field欠落時は不完全なsnapshotを
作らず停止する。exporterは`value`をcanonical JSONとして`HistoryRawRecord.text`にも
正確に投影する。したがって`history.get_current_state`は表示用summaryだけではなく、
capture時点のsecret-freeなexact valueを返す。後から値を推測・補完するprojectionや
別sourceへのfallbackは行わない。

```powershell
python -m tools.capture_system_snapshot `
  --spec C:\cutover\system-snapshot-spec.json `
  --output C:\cutover\system-snapshot.json
```

## 4. Import

```powershell
vsm migration import \
  --config /workspace/vsm.toml \
  --source /workspace/runs \
  --plan /workspace/migration-plan.json \
  --receipt /workspace/migration-receipt.json
```

import 直前に再 scan し、ファイル数、byte 数、manifest SHA-256 が dry-run と完全一致することを検証します。長文と生 payload は LETHE blob に置き、Event は blob ref を保持します。

## 5. Read-only archive

```powershell
vsm migration archive \
  --source /workspace/runs \
  --destination /workspace/legacy-archive \
  --plan /workspace/migration-plan.json
```

全 byte を digest 検証してコピーし、archive を read-only にします。archive 作成後も runtime reader は追加しません。
