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

## Coding escalation

明示 override は `gpt-5.6-luna/xhigh → gpt-5.6-sol/xhigh` です。失敗のたびに、

- Luna を続けた期待残 token
- WorkItem、未達 acceptance、gate 差分、artifact/decision ref だけを渡して Sol へ移る期待残 token

を再計算します。固定 retry 回数はありません。人工的な本番発火はせず、自然発生した Escalation Trace だけを計測します。

## Provider boundary

Claude の実装契約は公式公開境界だけを使います。

- [Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage)
- [Claude Code model and effort configuration](https://code.claude.com/docs/en/model-config)
- [Claude Code model configuration](https://support.claude.com/en/articles/11940350-claude-code-model-configuration)

provider 内部 classifier の仕様を Kernel invariant にしません。
