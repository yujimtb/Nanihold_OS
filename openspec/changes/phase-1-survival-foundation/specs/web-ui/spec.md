# web-ui

## ADDED Requirements

### Requirement: 観測 UI の常駐提供

システムは、Run 一覧・LiveTopology・イベントストリームを閲覧できる Web UI を自宅サーバー上で常駐提供しなければならない (SHALL)。

#### Scenario: 観測ビューが閲覧できる
- **WHEN** 自宅サーバー上で Web UI を開く
- **THEN** Run 一覧 / LiveTopology / イベントストリームが表示される

### Requirement: 外部非公開

システムは、Web UI を外部公開せず、LAN または VPN 内からのみアクセス可能にしなければならない (SHALL)。

#### Scenario: LAN 外からは到達できない
- **WHEN** LAN/VPN 外から Web UI に接続する
- **THEN** 到達できない
