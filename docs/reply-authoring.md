# 返信 authoring 経路

ACR-03 の返信経路は、エージェントが明示的に本文を作成してから、LETHE の
`write_supplemental` gateway へ `reply-draft@1` を投入する producer 経路です。
本文を作る自動生成ジェネレータはありません。

## 帰属

Dispatcher は `Agent_name.csv` から割り当てた個名を Execution、Pilot receipt、
WorkItem handoff の `agent_name` へ同じ値で渡します。エージェントはこの値を変更・
推測せず、supplemental envelope の `created_by=agent:<個名>` と `lineage` に
WorkItem ID、Execution ID とともに保存します。

## draft 投入

返信を作るエージェントは、対象の incoming Observation ID を
`derived_from.observations` に指定し、`channel`、`recipient`、`body`、
`drafted_at`（timezone-aware）を payload に設定して、明示的に一件の
`reply-draft@1` を投入します。body がない場合、または個名が handoff にない場合は
fail-fast します。

実装上の投入入口は `submit_reply_draft(gateway, submission)` です。
`ReplyDraftSubmission` はすでに書かれた本文から envelope を構築し、入口は既存の
汎用 `write_supplemental` gateway を一度だけ呼び出します。gateway へ渡す envelope
以外に、承認・送信の副作用はありません。

投入した draft は送信ではありません。エージェントは `reply-approval@1` を作成せず、
`send()` も呼びません。オーナーが承認した `reply-approval@1` だけを既存の
`lethe-channel-bridge` が配信し、その bridge が draft ID を参照する
`send-record@1` を作成します。したがって send-record から draft、Observation、
WorkItem、Execution、個名の監査線をたどれます。

承認済みカードの配信入口は bridge の既存 poller だけです。bridge は
`state=approved`、`automatic=false`、`approval_kind=reply-approval@1`、承認 ID の
存在を確認してから gateway の `reply()` / `send()` を呼び、配信後の
`send-record@1` を draft supplemental に anchor します。未承認カードをこの実装から
直接送信する経路はありません。

## コード境界

- `vsm/reply_authoring.py`: 明示的な body、Observation anchor、個名帰属を持つ
  `reply-draft@1` envelope の型付き構築と、既存 `write_supplemental` gateway への
  投入入口。
- `vsm/dispatcher.py`: assigned `agent_name` を executor 呼び出しへ伝搬。
- `vsm/pilot/production_host.py`: PilotHost request の WorkItem handoff に個名を保持。
- `scripts/production_pilot_host.py`: agent が draft→owner approval→既存 bridge の
  順序を守るための実行契約。

bridge の import、card-queue、approval、send-record の契約は変更しません。
