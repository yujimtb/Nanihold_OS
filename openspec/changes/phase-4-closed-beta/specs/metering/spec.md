# metering(N-25)

## ADDED Requirements

### Requirement: org 単位の原価計測

システムは、org 単位の原価(LLM・ストレージ・推論バッチ)を月次集計しなければならない (SHALL)。

#### Scenario: 月次原価が言える
- **WHEN** ある org の原価を問い合わせる
- **THEN** 「この組織を1ヶ月観測すると原価いくら」が言える(価格設計の入力になる)
