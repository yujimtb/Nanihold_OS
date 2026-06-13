# survival-report(N-7)

## ADDED Requirements

### Requirement: 日次生存レポートの自動投稿

システムは、残高(円)・当日/当月バーン・ランウェイ日数・稼働 Run/常駐 Node 一覧・失敗/WARN 集約・pending review 一覧を含む日次レポートを S4 定期 Run として生成し、Discord に投稿しなければならない (SHALL)。

#### Scenario: 3日連続で自動投稿される
- **WHEN** 3日間運用する
- **THEN** レポートが3日連続で自動投稿される

#### Scenario: 数値が projection と一致する
- **WHEN** レポートの数値を BudgetLedger・NodeStatus と照合する
- **THEN** 一致する
