# Design — Phase 3: 観測層統合

## Context

プロダクトの中核命題(組織をファジィに再構成し、人間がゆっくり確定していく)を実装する。
LETHE を汎用データ基盤に、Nanihold を構造推論エンジンにする。データ蓄積はカレンダー時間でしか
買えないため、セルフ観測の開始(8月中)を最優先する。

## Goals / Non-Goals

**Goals**
- マルチ org に耐える LETHE(tenant 分離・設定化・consent・retention・write-back)
- 「観測 → 構造表示 → 人間修正」のループ(N-20/21/22)
- 自動化レベルの土台(N-23)

**Non-Goals**
- マルチテナント実運用・β(Phase 4)
- 推論精度の定量目標(βで設定)。本フェーズは「修正ループが回ること」が目標
- FSX 数値最適化・公共性測定・共有剰余配分(post-launch)

## Decisions

- **SQLite はテナントごとにファイル分離**: スキーマ内分離より、(a) self-host 提供時の
  「あなたのデータはこのファイルだけ」という説明可能性 (b) 削除・持ち出しの容易さ
  (c) クロステナントバグの構造的排除、で勝る。
- **役割は排他ラベルではなく重み分布**: 同一人物が S1 0.6 / S3 0.3 でよい。
  「ファジィに再構成し、ゆっくり確定していく」製品方針のデータ表現そのもの。
- **推論は安価モデル + ルール中心**: 特徴抽出は LLM 不使用、役割分類のみ安価 LLM。
  集約は指数移動平均で急変を抑制。VSM 用語(S1..S5)は人間向け表示まで出さない。
- **HumanCorrection は教師として優先**: 人間が確定した役割を推論で上書きしない。
- **consent は保存前に執行**: オプトイン外は「保存してから隠す」のではなく「入れない」(L-7)。
- **削除は append-only と両立**: 削除イベントを記録した上で実データを物理削除する
  (削除した事実は残り、内容は残らない)(L-8)。
- **書き戻しの責務分担**: idempotency・承認・リトライは Nanihold、AuditLog と write は LETHE(L-9)。

## ObservedTopology データモデル(案)

```
ObservedPerson:  person_ref(LETHE)、org_id
ObservedRole:    person_ref × VSM 機能(S1..S5)× weight(0..1)× confidence × evidence(ProvenanceRef[])
ObservedEdge:    person_ref ↔ person_ref、種別(調整/指示/情報)、weight、evidence
ObservedUnit:    人の集合 + 推定された u-VSM 境界(部署・チーム相当)、confidence
HumanCorrection: 上記いずれかへの人間の確定・否定(O5 の出力。以後の推論で教師として優先)
```

## 自動化レベル(O6、§2.1)

| Lv | 許可 ToolEffect | 意味 |
|---|---|---|
| Lv0 観測のみ | PURE_READ, EXTERNAL_READ | 読むだけ |
| Lv1 提案 | + LOCAL_WRITE | 下書き生成、外部に出さない |
| Lv2 承認付き実行 | + EXTERNAL_WRITE(human review 必須) | 承認した書き込みのみ |
| Lv3 自動実行 | + EXTERNAL_WRITE(事後監査) | 範囲限定の自動書き込み。AuditLog 必須 |

レベルは Node(ObservedUnit)単位、初期値 Lv0、引き上げは human review 必須。
「組織全体を一括で Lv3 にする」操作は存在させない。

## Risks / Trade-offs

- **R12 O3 推論の品質不足**(検知: β月1評価)→ βの価値の重心を「可視化 + 人間修正」に置く。
  精度はループで漸進改善
- **R5 友人合流遅延**→ L 系を後ろ倒し可(β開始11月まで)。N 系は先行可能
