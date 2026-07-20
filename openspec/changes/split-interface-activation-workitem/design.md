# Design: split-interface-activation-workitem

## Context

WorkItem `work:interface-effective-activity-start` は「インターフェースが実効的に活動を開始する」ことを一項目で達成しようとしたため巨大化し、実行制限で完了できなかった。本 change は診断で判明した事実を背景に、既達条件を除いた残作業を実行可能な粒度へ割る。

## 診断で判明した事実(背景)

- 初回実行は `300` 秒で ProviderTimeout により停止した。
- 最初のモデル呼び出しだけで入力 14,349 トークンに達し、`token_budget = 12k` を単一呼び出しで超過していた。
- 実行系の budget 検査は turn 完了後にのみ行われ、turn の途中で停止しない。したがって既に超過している呼び出しでも走り切ってしまう。
- 実行 sandbox が設定と異なり read-only だった。Codex Desktop 側の managed permission の確認が必要。

これらのうち「12k を単一呼び出しで超過」「turn 途中で止まらない」は、巨大 WorkItem をそのまま再投入しても再び失敗することを意味する。したがって WorkItem 自体を小さく割るのが本 change の主眼である。

## 既達条件(再実行対象外)

- ReorientationAssessment の提示済み。
- owner 承認済みで ACTIVE。

これらは達成済みのため、分割後の WorkItem 群から除外する。

## 分割案の骨子(提案 — 最終はオーナー承認事項)

以下は骨子であり、確定リストではない。各項目は `1 WorkItem = 12k トークン / 300 秒で完了可能な粒度` を満たすよう更に細分し得る。

- (a) チャネルブリッジ検収 — nanihold_intercom の `add-lethe-channel-bridge` 完成物を仕様に対して検収する WorkItem。
- (b) 未完タスク棚卸しからの次期 WorkItem 起票 — 残タスクを列挙し、小粒 WorkItem として起票する管理 WorkItem。
- (c) 実装系タスク群 — (b) の棚卸し結果から派生する個別実装 WorkItem。1 項目が 12k/300 秒を超えそうな場合は起票時に更に分割する。

> オーナーがレビューすべき論点: 上記 (a)(b)(c) の分割で十分か、粒度は適切か、(c) の具体的内訳、および既達条件の認定(ReorientationAssessment 提示済み・ACTIVE)に漏れがないか。

## 決定と根拠

- **budget 制限値は緩めない**: 12k/300 秒は制約として維持する。制限を緩めるのではなく WorkItem を割ることで完了可能性を担保する。これにより実行系の budget 検査タイミング問題(turn 途中で止まらない)があっても、1 呼び出しが制限内に収まる設計へ寄せる。
- **実行系改修・sandbox 修正はスコープ外**: 診断事実として記すが、本 change は WorkItem 分割に限定する。turn 途中停止の実装と Codex Desktop managed permission 修正は別 change とする。

## リスクと対応

- **分割しても個別 WorkItem が 12k を超える**: acceptance criteria(WD-03)で「12k/300 秒で完了可能」を起票要件に縛り、超過見込みは起票時に再分割する。
- **既達条件の誤認**: 既達認定はオーナー承認事項とし、再実行対象からの除外を明示する(WD-02)。
