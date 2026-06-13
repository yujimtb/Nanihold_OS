# lethe-api-auth(L-1)

## ADDED Requirements

### Requirement: Bearer トークン必須化

システムは、すべての `/api/*` および `/admin/*` エンドポイントへのアクセスに有効な Bearer トークンを必須とし、`/health` と `/public/blobs/{sha256}` の扱いを明示的に決定・文書化しなければならない (SHALL)。

#### Scenario: トークンなしのアクセスは拒否される
- **WHEN** Bearer トークンを付与せずに `/api/*` または `/admin/*` を呼び出す
- **THEN** 401(認証エラー)を返し、保護対象データを一切返さない

#### Scenario: health は無認証で許可される
- **WHEN** トークンなしで `/health` を呼び出す
- **THEN** 死活確認の応答を返す(外形監視が叩けるよう無認証を維持)

#### Scenario: blob エンドポイントの扱いが文書化されている
- **WHEN** `/public/blobs/{sha256}` の認証要否を確認する
- **THEN** 署名付き URL か現状維持かの決定が README に記載されている

### Requirement: scope による認可

システムは、各トークンに scope(`read:persons` / `read:timeline` / `admin:sync`)を持たせ、scope 外の操作を拒否しなければならない (SHALL)。

#### Scenario: 不足 scope の操作は 403
- **WHEN** `admin:sync` を持たないトークンで `POST /admin/sync` を呼び出す
- **THEN** 403(認可エラー)を返し、sync を実行しない

#### Scenario: 正しい scope の操作は許可される
- **WHEN** `read:persons` を持つトークンで persons 照会を行う
- **THEN** FilteringGate(lethe-filtering-gate)通過後の persons データを返す

### Requirement: トークン管理とローテーション

システムは、トークンを `.env` 経由で供給し、ローテーション手順を README に明記しなければならない (SHALL)。

#### Scenario: ローテーション手順が文書化されている
- **WHEN** 公開作業前に鍵をローテーションする必要が生じる
- **THEN** README にローテーション手順が記載されており、その手順で再発行できる
