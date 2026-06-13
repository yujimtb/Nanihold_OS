# lethe-schema-config(L-5)

## ADDED Requirements

### Requirement: org 別スキーマの設定化

システムは、寮固有の person page 構成・property を org ごとの宣言的定義(TOML/YAML)に外出しし、property 解決(大文字小文字・空白の揺れ吸収)を設定駆動にしなければならない (SHALL)。

#### Scenario: 設定差替えで別スキーマになる
- **WHEN** 研究コミュニティ用(研究テーマ・所属・発表、HUMAI 想定)の設定に差し替える
- **THEN** 設定ファイルの差し替えだけで対応する person page が生成される
