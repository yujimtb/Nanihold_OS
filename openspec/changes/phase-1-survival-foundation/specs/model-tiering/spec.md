# model-tiering(N-19)

## ADDED Requirements

### Requirement: 役割別モデル階層

システムは、S2/S4 の定常処理・分類・ルーティングに安価モデル、S1 の納品物生成・S3* 検証に高性能モデルを割り当て、LiteLLM のモデルエイリアスで役割名と実モデルを分離しなければならない (SHALL)。

#### Scenario: config 1行で差し替えられる
- **WHEN** ある役割の実モデルを変更する
- **THEN** config 1行で差し替えられる

### Requirement: モデル別コスト内訳

システムは、日次レポートにモデル別コスト内訳を表示しなければならない (SHALL)。

#### Scenario: 内訳が出る
- **WHEN** 日次レポートを見る
- **THEN** モデル別のコスト内訳が表示される
