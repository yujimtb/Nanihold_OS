# Bayesian routing

## Candidate identity

統計の一意キーは次の canonical hash です。

`adapter@version + provider + selection + effort + toolset + sandbox/environment`

`selection`はcoding用のexact model snapshotまたはInterface用の
`provider_configured`です。Interface candidateへ人格名や暫定モデル名を含めず、
providerが報告したactual modelは各outcomeの証拠として保存します。exact selectionの
model名だけの統計へ環境の異なる結果を混ぜません。Anthropic は同じ agent でも infrastructure 設定差で Terminal-Bench 2.0 score が最大 6 point 動いた事例を公開しており、environment を候補 identity に含める根拠になります。

## Prior と尤度

public benchmark は事前分布としてだけ使い、Nanihold の対象環境で検証した結果を尤度として更新します。

- coding: [Artificial Analysis coding agents methodology](https://artificialanalysis.ai/methodology/coding-agents-benchmarking)、[Terminal-Bench 2.1](https://www.tbench.ai/news/terminal-bench-2-1)、[SWE-bench Verified](https://www.swebench.com/verified.html)
- tool use: [Berkeley Function Calling Leaderboard](https://gorilla.cs.berkeley.edu/leaderboard)
- 少数標本で prior と実績を結ぶ構造: [BayesRouter](https://openreview.net/forum?id=K3xNTJOM1j)
- environment noise: [Anthropic infrastructure noise](https://www.anthropic.com/engineering/infrastructure-noise)

各 prior は source、benchmark family、version、sample count、harness、success/failure、log metric sample を保存します。

posterior:

- success: Beta-Binomial
- log token、cost、latency: Normal-Inverse-Gamma
- AI Judge: deterministic/human truth に対する confusion matrix

## 三つの目的

- `reliability_then_cost`
- `expected_utility`
- `quality_max`

三つを常に計算・表示し、本番は現在 `quality_max` です。AI Judge だけの候補は production 選択できません。outcome Event が追加されると evidence cursor が変わるため、古い RouteSnapshot は再承認が必要です。

公開benchmark priorだけでverified outcomeがまだない初期状態は、独立S3*とownerの承認を
持つ`PUBLISHED` RouteSnapshotに限ってbootstrapできます。verified outcomeが一件でも
追加された後、それらがすべてcheap AI Judge由来でdeterministic/human検証がゼロなら、
production選択をfail-fastします。`verified_samples=0`をAI Judge evidenceとして数えません。

## RouteSnapshot lifecycle

同一`route_key`でroutableなsnapshotは一つだけです。通常の切替は後継を
`DRAFT → S3_STAR_APPROVED → OWNER_APPROVED`まで進め、旧`PUBLISHED`を
`superseded_by_approved_snapshot`理由のhuman Eventで`RETIRED`にしてから、後継を
別commandで`PUBLISHED`にします。旧版のretirementと後継publishを黙って一操作へ
まとめません。後継candidateのregistry登録とcurrent evidence cursorはretirement時にも
再検査します。

後継のないroute廃止は`route_decommissioned`を使い、
`replacement_snapshot_id=null`を明示します。`RETIRED`はdispatcherの選択対象外で、
scoreも再計算しません。別の`PUBLISHED`が残るrouteへのpublishはfail-fastし、先に
明示retirementを要求します。

## Coding escalation

本番 route `coding:personal-production` の候補は
`gpt-5.6-luna/xhigh` を第一候補、`gpt-5.6-sol/xhigh` を明示 override 先として
登録します。通常の route 選択は Luna から始め、Sol を候補順位の競争相手として
通常選択しません。明示 override は `gpt-5.6-luna/xhigh → gpt-5.6-sol/xhigh` です。
Luna の実行が自然発生した失敗（未達 acceptance または gate 差分を含む）になった
ときだけ、失敗ごとに、

- Luna を続けた期待残 token
- WorkItem、未達 acceptance、gate 差分、artifact/decision ref だけを渡して Sol へ移る期待残 token

を再計算します。Sol 移行側の期待残 tokenが小さい場合だけ overrideし、それ以外は
Luna継続を選びます。固定 retry 回数はありません。人工的な本番発火はせず、自然発生した
Escalation Trace だけを計測します。

## Provider boundary

Claude の実装契約は公式公開境界だけを使います。

- [Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage)
- [Claude Code model and effort configuration](https://code.claude.com/docs/en/model-config)
- [Claude Code model configuration](https://support.claude.com/en/articles/11940350-claude-code-model-configuration)

provider 内部 classifier の仕様を Kernel invariant にしません。
