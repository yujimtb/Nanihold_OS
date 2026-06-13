# sales-intake(B-4)

## ADDED Requirements

### Requirement: 商品定義ポリシー

システムは、受ける/受けない範囲と価格方針を S5 ポリシーとして明文化し、定義外の受注を human review 必須としなければならない (SHALL)。

#### Scenario: 定義外の受注は review される
- **WHEN** 商品定義外の受注が来る
- **THEN** human review が要求される

### Requirement: 受注導線

システムは、受信を LeadRegistered として取り込み、S4 が見積もりドラフトを生成し、human review を経て EstimateIssued を発行する導線を提供しなければならない (SHALL)。

#### Scenario: lead から見積もりまで流れる
- **WHEN** 受注問い合わせを受信する
- **THEN** LeadRegistered → 見積もりドラフト → human review → EstimateIssued が流れる
