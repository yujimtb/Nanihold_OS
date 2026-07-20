# Nanihold OS

Nanihold OS は、AI Pilot が終了・交代しても、ユーザー所有の主体、会話、約束、仕事、監査証跡が残る u-VSM control plane です。

最上位要件は、ユーザーが同じ相手との長期的な対話として運用でき、短い訂正だけで作戦を継続でき、完了を検証できることです。token 削減はこの体験を満たした後の制約です。

## 構造

| 層 | 所有する責務 |
|---|---|
| Nanihold Kernel | DataSpace、再帰的 UVSMNode、WorkItem、Execution、Event、Grant、Budget、Effect Lease、監査・制御 policy |
| Agent / Pilot | provider、model、effort、session、quota、Claude 動作 mode、Bayesian routing、PilotHost |
| Interface | WebUI、CLI、限定的な chat surface、通知。表示都合を Kernel 型へ持ち込まない |
| LETHE | DataSpace ごとの唯一の Event Ledger 正本、content-addressed blob、Projection、検索、長期記憶 |

永続する対話主体はユーザー所有の Interface Node と canonical Conversation です。そこへ接続する Interface Pilot は交換可能であり、人格名や provider のモデル固有名を持ちません。現在の Claude Adapter は provider 側のモデル設定を使用し、Interface 用 effort を `high` に固定します。Pilot が変わっても Node、会話、決定、約束、未完 WorkItem は変わりません。

仕事は Node Tree、Work Graph、Event Ledger の三つで表現します。

- Node Tree は組織と再帰的な S1–S5/S3* を保持します。
- Work Graph は委任に `DEPENDS_ON` と `INTEGRATED_BY` を加え、親へ統合責任、子へ実行義務を残します。
- Event Ledger は順序、因果、optimistic stream version、idempotency を保持します。

詳細は [architecture.md](docs/architecture.md) を参照してください。

## 安全性

- LETHE へ Effect 計画を確定できるまで副作用を開始しません。
- 結果不明の Effect は成功と推定せず、Effect idempotency key による reconciliation を要求します。
- 人間介入は影響する WorkItem、Execution、Effect Lease だけを停止します。
- 重大な S3* finding は同階層 S5 の明示的 risk acceptance まで統合を止めます。
- `completed` は acceptance、必須 test、blocking deviation なし、独立 S3* gate、integration merge、remote push の全成立後だけです。
- backend、DataSpace、Pilot mode、sandbox 証明、RouteSnapshot が欠ける場合は起動前に失敗します。backend fallback、mode fallback、model substitution はありません。

## Token と UX

- owner message を応答前に personal Lake へ保存します。
- 通常 turn は高価な Interface Pilot を最大一回だけ呼び、表示、指示、決定、約束を同じ構造化応答から得ます。
- status、polling、keepalive、routing 通知は Projection と決定論的ロジックで返します。
- provider session 継続時は resume と event delta を使い、全履歴を再送しません。
- classifier、model substitution、context reload、model-call polling、false-complete は一件で Token Efficiency Lab の調査対象です。
- Token Lab の試験はロジック優先です。モデルが必要な場合も、検証用の安価な exact candidate allowlist と `low` effort の組合せだけを許可します。暫定的なモデル名を禁止語として判定しません。

119 件の過去 owner 入力を個人情報を含まない fingerprint manifest として replay し、短い訂正、離席・再開、約束、複数仕事、停止、完了説明の契約を検証します。

## 開発と試験

標準環境は WSL + Docker Compose の `app` service です。Windows の Python 環境は使いません。

```powershell
$env:NANIHOLD_WSL_PROJECT_DIR='/mnt/d/userdata/docs/projects/Nanihold_OS'
.\codex-dev.cmd compose run --rm app python -m pytest -q
.\codex-dev.cmd compose run --rm app vsm --help
```

WebUI:

```powershell
docker compose --profile runtime up --build api web
```

安価な検証用 exact candidate だけを使い、実Claude Codeまで含めた隔離ローカル確認:

```powershell
.\local-review.cmd init
.\local-review.cmd up
.\local-review.cmd token
```

`up` が表示した動的localhost URLを開きます。検証候補は
設定で明示した安価なexact candidate allowlist、`low`、`observe_only`、tools disabledで、
モデルはConversation送信時だけ呼ばれます。詳しくは
[local-verification.md](docs/local-verification.md) を参照してください。

起動前に次を用意します。

1. `config/nanihold.example.toml` を `vsm.toml` へコピーする。
2. `EXAMPLE-REPLACE` の benchmark 証拠、fingerprint、sandbox certificate を実値へ置換する。
3. `.env.example` の必須値を `.env` へ設定する。
4. `vsm routes models` で exact candidate key を確認する。
5. verified outcome を記録し、S3*、owner の順で RouteSnapshot を公開する。

運用手順は [operations.md](docs/operations.md)、API は [api.md](docs/api.md)、一回限り移行は [migration.md](docs/migration.md) にあります。

## 文書

- [設計正本](docs/architecture.md)
- [API 契約](docs/api.md)
- [運用と commissioning](docs/operations.md)
- [隔離ローカル確認環境](docs/local-verification.md)
- [旧成果物の一回限り移行](docs/migration.md)
- [Bayesian routing と benchmark](docs/routing.md)
- [実装・検証状況](docs/implementation-status.md)
- [今後の順序](ROADMAP.md)

古い設計 PDF は設計入力として参照しますが、runtime reader と配布物には含めません。
