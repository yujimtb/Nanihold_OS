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
