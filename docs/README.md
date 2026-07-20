# Nanihold OS documentation

| 文書 | 正本の範囲 |
|---|---|
| [architecture.md](architecture.md) | 層、型、u-VSM、永続性、不変条件 |
| [api.md](api.md) | REST / WebSocket 公開契約 |
| [operations.md](operations.md) | 設定、Pilot mode、commissioning、障害時手順 |
| [production-pilot-host.md](production-pilot-host.md) | generic Interface Pilotとcoding Pilotのproduction device境界 |
| [local-verification.md](local-verification.md) | cheap exact candidate allowlistによる隔離検証 |
| [migration.md](migration.md) | 所有先確定、dry-run、import、read-only archive |
| [routing.md](routing.md) | Bayesian posterior、benchmark provenance、公開 gate |
| [implementation-status.md](implementation-status.md) | 実装済み範囲と検証結果 |
| [deploy/ha/README.md](../deploy/ha/README.md) | PC内HAの入力契約、静的gate、RPO/RTO境界 |

旧文書の API や state machine は仕様ではありません。現在の Python 型、Event、上記文書だけを正本とします。
