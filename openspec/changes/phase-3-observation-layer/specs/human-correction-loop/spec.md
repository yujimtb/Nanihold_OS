# human-correction-loop(N-22)

## ADDED Requirements

### Requirement: O5 確認ループ UI

システムは、ObservedTopology のグラフ上で各推定(役割・辺・ユニット境界)を「合っている/違う(正しくは…)/わからない」で記録し HumanCorrection イベントを発行する UI を、1判断10秒以内で操作できるよう提供しなければならない (SHALL)。

#### Scenario: 修正が即時反映され尊重される
- **WHEN** ある推定を修正する
- **THEN** 即時に表示へ反映され、次回推論バッチで尊重される(E2E)
