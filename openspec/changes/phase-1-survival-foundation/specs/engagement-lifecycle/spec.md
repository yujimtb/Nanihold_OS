# engagement-lifecycle(N-13 + N-14)

## ADDED Requirements

### Requirement: 受託ライフサイクルイベント

システムは、LeadRegistered → EstimateIssued → OrderAccepted(human review 必須)→ DeliverySubmitted → AcceptanceRecorded → InvoiceIssued → PaymentReceived の受託ライフサイクルを、customer_id・金額・関連 Run 参照付きで記録しなければならない (SHALL)。

#### Scenario: 顧客別損益が表示される
- **WHEN** チャットボット案件(過去分)とサブスクを遡及登録する
- **THEN** 顧客別損益が表示される

#### Scenario: 受注は承認を経る
- **WHEN** OrderAccepted を発行する
- **THEN** human review を経る

### Requirement: 請求・入金の最小記録

システムは、請求・入金を手動 CLI で InvoiceIssued/PaymentReceived として記録し(自動化しない)、日次レポートに「未請求の検収済み案件」「入金待ち請求」を表示しなければならない (SHALL)。

#### Scenario: 請求漏れが構造的に検出される
- **WHEN** 検収済みで未請求の案件がある
- **THEN** 日次レポートに「未請求の検収済み案件」として表示される
