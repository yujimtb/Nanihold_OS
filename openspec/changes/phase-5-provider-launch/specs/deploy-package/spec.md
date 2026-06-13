# deploy-package

## ADDED Requirements

### Requirement: self-host デプロイパッケージ

システムは、docker compose 一式とセットアップ文書を self-host 提供形態の標準として整備しなければならない (SHALL)。

#### Scenario: 標準手順でセットアップできる
- **WHEN** self-host で環境を構築する
- **THEN** docker compose 一式とセットアップ文書だけで立ち上がる

### Requirement: 運用ドキュメント3種

システムは、管理者ガイド・メンバー向け説明(同意の意味)・障害時 FAQ の3種を整備しなければならない (SHALL)。

#### Scenario: 3種が揃っている
- **WHEN** 提供開始前にドキュメントを確認する
- **THEN** 管理者ガイド / メンバー向け説明 / 障害時 FAQ の3種が揃っている
