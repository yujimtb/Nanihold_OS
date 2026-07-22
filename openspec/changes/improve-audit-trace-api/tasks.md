# Tasks: improve-audit-trace-api

> 承認済み実装 change。主経路は design.md の案B1であり、LETHE側の相関/type索引には依存しない。

## Track A. 仕様・設計

- [x] A1 並列障害(2件並行 → 両方無応答)の原因と対処方針を記述する
  - Spec: ATA-01, ATA-02
- [x] A2 クライアント切断後の協調キャンセルと並行度上限を記述する
  - Spec: ATA-02, ATA-01
- [x] A3 `_events()` のcursor 0全走査を性能根拠付きで記述する
  - Spec: ATA-03
- [x] A4 LETHE依存の案Aと依存なしの案Bを比較し、案B主経路を決定する
  - Spec: ATA-03
- [x] A5 canonical全走査との等価性、read-only、派生性を記述する
  - Spec: ATA-04
- [x] A6 wire契約・公開インターフェース・既存テストの非破壊条件を記述する
  - Spec: ATA-05

## Track B. 実装

- [x] B1 `AuditTraceService` の本番経路を `stream()` 直引きへ変更し、`page()`全走査を廃止する
  - Spec: ATA-03
- [x] B2 canonical `_events()`参照と新経路の差分検証を追加する
  - Spec: ATA-04
- [x] B3 audit-tracesルートをasync化し、有界thread offloadと設定値ベースの同時実行上限を追加する
  - Spec: ATA-01
- [x] B4 Request切断監視とストリーム境界の協調停止を追加する
  - Spec: ATA-02
- [x] B5 audit traceの並行度/SLO設定をconfig・runtime・サンプルへ配線する
  - Spec: ATA-01, ATA-03

## Track C. 検証・文書

- [x] C1 `openspec validate improve-audit-trace-api --strict` を通す
- [x] C2 約5万イベントの性能テストで全走査をせず数秒台を確認する
  - Spec: ATA-03
- [x] C3 2件並行成功、容量超過503、切断協調停止をテストする
  - Spec: ATA-01, ATA-02
- [x] C4 既存テスト全体、wire契約、read-onlyを確認する
  - Spec: ATA-05
- [x] C5 実装に合わせてアーキテクチャ・運用説明を更新する

## 未実施の将来拡張

- [ ] D1 skcollege_database側の相関/type/keyset索引を使う案A fast-pathを別changeで確定する
  - Spec: ATA-03
