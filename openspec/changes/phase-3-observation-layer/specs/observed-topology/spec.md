# observed-topology(N-20)

## ADDED Requirements

### Requirement: ObservedTopology projection

システムは、ObservedPerson/ObservedRole/ObservedEdge/ObservedUnit/HumanCorrection を持つ ObservedTopology projection を構築し、役割を排他ラベルではなく重み分布(同一人物が S1 0.6 / S3 0.3 等)で表現しなければならない (SHALL)。

#### Scenario: 信頼度付きで表示される
- **WHEN** LETHE timeline からの派生イベントを処理する
- **THEN** projection が構築され、Web UI に信頼度付きで表示される

### Requirement: evidence による provenance 保持

システムは、各 ObservedRole/ObservedEdge に evidence(ProvenanceRef[])を持たせ、推定の由来を辿れるようにしなければならない (SHALL)。

#### Scenario: 推定の由来を辿れる
- **WHEN** ある推定役割を選択する
- **THEN** 由来となった observation 参照(evidence)が確認できる
