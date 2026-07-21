# Design: add-agent-comm-routing

## Context

着信は `lethe-channel-bridge` を経て全て LETHE に取り込まれるが「誰宛か」の概念がなく、エージェント(Node / Pilot 実行主体)を名指した連絡を当人へ届ける経路も、エージェント自身が返信を書いて投入する経路もない。返信は `reply-approval@1` → ブリッジ send という配信面はあるが、起草面が欠けている。加えて、実行主体は `node_id` / `pilot_id` の機械 id でしか追えず、「どのエージェントが何をやっているか」の人可読な帰属がない。

オーナー要望(2026-07-21):「返信は自動生成ではなく、各エージェントに対して疎通する通知システムと返信システムにする」。同日夕の確定事項(LETHE decision sup:c8e91a37 / sup:b3f7d215)で、宛先記法・通知配送基盤・命名の自動割当・Nagi 常設席が確定した。本設計は通知(inbound routing)と返信(outbound authoring)、および個名の自動割当を、既存資産の上に最小の新規機構で載せる。

## 既存資産の再利用

```
着信  ─▶ lethe-channel-bridge(observation 取込・登録済みチャネル)─▶ LETHE
                                                                     │
                                    [新規] inbound routing:宛先(@名前/継承)を解決
                                                                     ▼
                    Nanihold Operational Ledger(=personal-primary LETHE :8080,
                    space:personal-primary)へ通知イベント配送 ──(必要時)──▶ WorkItem 起票
                                                                     │
                          WorkItem dispatch 時に実行エージェントへ個名を自動割当
                                        (規模↔階級 / 言語↔系統 / いいね規則)
                                                                     ▼
                                          エージェントが返信文を起草(自動生成しない)
                                                                     │
                          [新規] 帰属付き reply-draft@1 を card-queue へ投入
                                                                     │
                                            reply-approval@1(オーナー承認・既存)
                                                                     ▼
                            lethe-channel-bridge の reply()/send()(既存経路)─▶ 相手
```

- 名前プール(正): `D:\userdata\docs\projects\_cutover_20260720_fable_activation\asset\Agent_name.csv`(131 行、列: カテゴリ[居/糸/器/水/木/天候/地]・規模 1-3・意味座標・日本語ローマ字[Toki/Aki/Irie/Nagi/Mio/Kumo 等]・英名・ラテン名・いいねフラグ)。
- 既存機構: LETHE card-queue(`reply-draft@1` → `reply-approval@1` → `send-record@1`)、Intercom ブリッジ、Nanihold の WorkItem / Execution / Node、Interface node(`node:owner-interface`)、pilot(`codex-coding-s1` 等)。

## 宛先記法(確定 / ACR-02)

- **主**: 本文文頭の `@名前`(例: `@Toki ...`)。
- **補**: リプライ / スレッドでの宛先継承(親メッセージの宛先を子へ引き継ぐ)。
- **プレフィックス文字は設定値**: Intercom 設定 `AGENT_ADDRESS_PREFIX`(既定 `"@"`)から解決し、コードにハードコードしない。設定変更で規約プレフィックスが変わる。
- **非合致は観測のみ**: どの宛先規約にも合致しない着信は特定エージェントへ配送せず、既存の LETHE 観測取り込みに留める(取りこぼしは誤配送より安全 = fail-safe)。

> 経緯: 旧設計では宛先規約を「A. 先頭『名前:』/ B. メンション / C. エイリアス」の 3 案比較で提示し確定をオーナー承認事項としていた。2026-07-21 夕の確定で「文頭 `@名前` 主 + 継承 補、プレフィックスは設定値」に決定したため、比較は破棄し確定仕様のみを記す。

## 通知配送基盤(確定 / ACR-02)

- **基盤**: Nanihold Operational Ledger(実体 = personal-primary LETHE `:8080`、`space:personal-primary`)のイベントとして通知を配送する。疎結合・監査自然(ACR-04 と直結)・エージェント未起動でも滞留可。
- **昇格**: 返信起草という作業単位を要する通知は WorkItem 起票へ昇格させる(二段構え)。既存実行系にそのまま乗り、粒度管理と整合する。

> 経緯: 旧設計では配送形態を「1. Ledger イベント / 2. Execution 注入 / 3. WorkItem 起票」の 3 案比較で提示していた。確定で「Ledger イベント基盤 + 必要時 WorkItem 昇格の二段構え」に決定(Execution 注入は見送り)。

## 命名の自動割当(確定 / ACR-01, ACR-06)

WorkItem dispatch 時に、実行エージェントへ個名を自動割当する。タスクごとに新規付与し、名前プールをローテーションする。目的は「どのエージェントが何をやっているか」の可視化(Ledger / receipt / チャネル通知に名前を刻む)。

- **規模 ↔ モデル階級**:

  | 規模 | 階級 | GPT 系 | Claude 系 |
  | --- | --- | --- | --- |
  | 3 | 旗艦 | `sol` | `Fable` |
  | 2 | 中堅 | `terra` | `Opus` |
  | 1 | 軽量 | `luna` | `Sonnet` / `Haiku` |

- **言語 ↔ 系統**: Claude 系 = 日本語名(`日` 列)/ GPT 系 = 英名(`英` 列)/ その他プロバイダ = ラテン名(`羅` 列)。
- **いいねフラグ**: `いいね` = 0 の行(重複マーク)は使用禁止。空欄と 1 は使用可。
- **枯渇時**: 条件に合致する未使用名が尽きたら数字サフィックス(例: `Hayate2`)を付して継続。
- **エフォートレベルは命名と無関係**(別管理)。命名選定に用いない。
- レジストリは個名 ↔ `node_id` / `pilot_id` を保持し、Ledger / receipt / 通知へ帰属として刻む。

### Nagi = S5 常設席(ACR-06)

- Interface node(`node:owner-interface`)の割当名 `Nagi`(凪、`Agent_name.csv` 天候/規模3/凪 行)は**手動割当済みの予約名**で、ローテーション対象外(自動割当の候補に含めない)。
- `Nagi` は **S5(最上位)としてタスク完了条件(終了条件)を持たない常設の席**として扱い、WorkItem の受け入れ条件体系の外に置く。
- 名前は席に属し、搭乗するパイロット(実行主体)の交代があっても不変。

> 経緯: 旧設計は「システムは名前プールから個名を自動割り当てしてはならない / 割り当てはオーナーの明示決定」を原則としていたが、確定でこれを撤回し、ローテーション自動割当に転換。`Nagi` のみ手動予約席として例外化。

## 監査(ACR-04)

- 通知配送: 着信 id → 宛先個名 → 配送形態(Operational Ledger イベント id / WorkItem id)を receipt で連結。
- 返信帰属: `reply-draft@1` に書き手個名を帰属付与 → `reply-approval@1` → `send-record@1` を、応答先着信 id で貫通トレース。
- 割当帰属: dispatch で付与した個名 ↔ WorkItem ↔ receipt を連結。
- これにより「誰が誰宛の連絡に、誰の承認で返したか」「どのエージェントがどの作業を担ったか」を後から復元できる。

## スコープ外(明示)

- 返信文の自動生成、承認レス自動送信。
- `lethe-channel-bridge` の import / card-queue / send 契約の変更。
- LETHE 側 projection・承認 UI の実装。
- エフォートレベルと命名の連動。

## リスクと対応

- **誤配送**: 宛先記法の曖昧一致。合致しない着信は特定エージェントへ配送せず観測に留める(fail-safe)。
- **通知の滞留**: エージェント未起動時。Operational Ledger 基盤なら滞留可、返信が要るものは WorkItem 昇格で可視化。
- **個名の枯渇・衝突**: いいね=0 行の除外で候補を絞りつつ、枯渇時は数字サフィックスで継続。`Nagi` はローテーションから除外し予約席として保護。
- **プレフィックスのハードコード回帰**: `AGENT_ADDRESS_PREFIX` を単一の設定源とし、判定ロジックが設定値以外を参照しないことをレビュー観点に含める。
