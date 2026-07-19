# Nanihold OS commissioning roadmap

## 現在の cutover

コードは Node / WorkItem / Execution / Event を中心とする新 Kernel へ破壊的に切り替えました。旧 payload、alias、読取経路はありません。既存成果物は runtime から隔離し、所有先を明示した一回限り import と digest 固定 archive だけを許可します。

## Commissioning gate

1. LETHE の対象 DataSpace を作成し、backend と location を固定する。
2. owner Interface Node と必要な会社・実験 Node を登録する。
3. personal Lake と company Lake の境界、必要な ReferenceGrant を承認する。
4. SandboxProfile と PilotHost device identity を発行する。
5. public benchmark prior と検証済み outcome を登録する。
6. S3*、owner の順で RouteSnapshot を承認し、現在の evidence cursor で公開する。
7. legacy migration を dry-run し、全 source の DataSpace、Node、Conversation 所有先を確定する。
8. import receipt と archive digest を dry-run manifest と一致させる。
9. 119 件 UX golden、全ロジック試験、frontend build、独立 S3* gate を通す。
10. integration branch へ merge して remote push を確認する。

production deploy はこの roadmap の対象外です。

## 運用後の resident S4

Token Efficiency Lab は削減構造の commissioning 後に resident S4 として運用します。常時モデルを起動せず Event と週次ロジックで監視します。

即時調査:

- permission classifier
- requested/actual model mismatch
- full-history resend
- model-call polling
- false-complete

統計調査:

- 同種 WorkItem 20 件以上
- 承認済み平均から input token が 10% 以上悪化

合格目標:

- 総 input token 50% 以上削減
- 高価な Interface input 70% 以上削減
- UX golden 119/119
- model-call polling 0
- `sandboxed_bypass` classifier 0
- full-history resend 0
- false-complete 0

実トラフィックの削減率は production evidence が揃うまで未達扱いです。ロジック試験の合格を利用枠の実測値へ読み替えません。
