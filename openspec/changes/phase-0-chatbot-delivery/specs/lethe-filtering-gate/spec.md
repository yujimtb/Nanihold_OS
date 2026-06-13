# lethe-filtering-gate(L-2)

## ADDED Requirements

### Requirement: 全取得経路の FilteringGate 通過

システムは、回答生成に使われるすべての取得経路(persons / slides / messages / timeline)を FilteringGate に通過させなければならない (SHALL)。

#### Scenario: 各取得経路が Gate を経由する
- **WHEN** persons / slides / messages / timeline のいずれかを取得する
- **THEN** その経路は FilteringGate を経由し、未通過の生データが回答生成に渡らない

### Requirement: Visibility / consent によるデータ層遮断

システムは、`Visibility=false` の person と consent 範囲外フィールド(DoB・出身地等)を API レスポンスから構造的に除外し、遮断をプロンプトではなくデータ層で行わなければならない (SHALL)。

#### Scenario: Visibility=false の person が漏れない
- **WHEN** `Visibility=false` のダミー person に関する質問への回答を生成する
- **THEN** その人物に関する情報が API レスポンス・回答に一切含まれない

#### Scenario: consent 範囲外フィールドが含まれない
- **WHEN** consent 範囲外の DoB・出身地を含む可能性のある照会を行う
- **THEN** 当該フィールドがレスポンスデータに構造的に含まれない(プロンプトでの隠蔽に依存しない)

### Requirement: identities 非表示の経路適用

システムは、既存の identities 非表示挙動を、チャットボット経路でも有効にしなければならない (SHALL)。

#### Scenario: チャットボット経路で identities が露出しない
- **WHEN** チャットボット経路から人物関連の回答を生成する
- **THEN** identities(連絡先等の識別子)がレスポンス・回答に露出しない
