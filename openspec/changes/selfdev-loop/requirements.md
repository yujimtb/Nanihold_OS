# 自己開発ループ 要件定義 v1(Fable、2026-07-13)

## 0. 目的と一言定義
Nanihold OS が、自分自身のリポジトリに対する改善を「**提案 → 合議承認 → ブランチを切って隔離実装 → 独立検証 → 最終合議 → マージ準備**」の閉ループとして自律実行できるようにする。これが自律稼働する組織OSとしての次の大目標である。v1 の到達点は「人間の最終マージだけを残して、ループ全体が無人で回る」こと。自動 push・自動 merge は v1 では行わない(selfhost-plan の原則「Naniholdが自分を開発することと、自分を無条件に信頼することを分離する」を維持)。

## 1. 前提となる既存資産(実装済み・実装中)
- AgentRuntime(claude-code/codex/litellm/fake、ロール別設定)、Budget、S2 AI調停、Algedonic、**Consortium(ラウンド制・Human招待・timeout両ポリシー)**、context view+セッション再開
- RunManifest+Run毎worktree隔離+1 Run 1 writer(mission/selfdev-workspace)、trusted GateRunner G1-G4(mission/gate-runner)
- 実装中: durable resume+quota circuit breaker(mission/selfdev-resume)
- WebUI: 対話コンソール(Run化/指示化ブリッジ)、ライブ組織図ツリー+介入

## 2. ループの正式な状態機械(要件)
Proposal は次の状態を持ち、全遷移を Event_Log に記録し WebUI で追跡できること:

PROPOSED → CONSORTIUM_REVIEW → APPROVED | REJECTED | NEEDS_HUMAN
APPROVED → WORKSPACE_READY(selfdev/<proposal_id> ブランチ+worktree作成)
→ IMPLEMENTING(実装Run実行中)→ GATES_RUNNING → GATES_PASSED | GATES_FAILED
GATES_FAILED →(修正Run 1回のみ)→ GATES_RUNNING | ABORTED(+algedonic)
GATES_PASSED → AUDIT(S3★独立監査)→ FINAL_CONSORTIUM → MERGE_READY | REJECTED_FINAL
MERGE_READY →(人間が merge/push)→ DONE / (人間が却下)→ ARCHIVED

- どの状態からも人間・Fable は ABORT・SUSPEND できる(既存介入APIに統合)
- ABORTED / REJECTED でも worktree の diff は candidate.patch として監査保存してから掃除する(既存 WorkspaceController)
- 起動時の Proposal 単位 integrity 隔離で `NEEDS_HUMAN` になった Proposal は、通常の Consortium Human waiter と別の解決対象として扱う。Human の reject または control abort は `ABORTED` へ遷移して active slot を解放し、隔離対象 artifact の cleanup 失敗で解決を閉塞させない。approve を受けた場合だけ明示的な integrity 解決を記録して `APPROVED` へ戻し、controller が再開を試みる。

## 3. 提案(Proposal)の要件
- ProposalManifest: id / title / 動機(なぜ今это必要か)/ 変更scope(許可path)/ 受入条件(検証可能な形式)/ risk class(low・normal・protected)/ 予算見積(トークン・時間)/ 提案元(conversation_id・decision・roadmap項目参照)/ 依存する先行Proposal
- 提案の入口は3つ: (a) ready-queue(ROADMAP/openspecから抽出した実装候補)、(b) 人間・Fableが対話コンソール/APIから、(c) S4環境スキャンの発見(テスト失敗・監査所見など)から
- **protected path**(AGENTS.md / .github / vsm/gates / vsm.toml / openspec原本 / 本要件定義由来のspec)を scope に含む提案は risk=protected とし、Consortium 承認に加えて**人間の明示承認がなければ APPROVED にならない**

## 4. 合議(Consortium)の要件 — オーナー指定の中核
- 承認合議の参加者: **S3(資源・スケジュール観点)、S4(環境・影響観点)、S5(方針・仕様適合観点)**+Human(invited、timeout時は risk=low のみ proceed、それ以外は abort)
- 既存 Consortium 実装(ラウンド制・意見表明・反対意見要約付き決定)をそのまま使い、審議材料として ProposalManifest+関連 context view を与える
- 最終合議(FINAL_CONSORTIUM)の審議材料: diff 要約、gate_report.json、S3★監査所見、予算実績 vs 見積。承認出力には「マージ推奨理由・残リスク・反対意見の要約」を必ず含み、そのまま PR 説明文に転記できる形式とする
- 合議の決定はイベント(consortium_decided)として Proposal に紐付け、WebUI から全文を閲覧できること

## 5. 実装Runの要件
- APPROVED になったら controller が base_sha(最新main)から selfdev/<id> ブランチ+worktree を作成し、RunManifest に Proposal を承継(scope・受入条件・予算)
- 実装は S1(codex gpt-5.6-luna xhigh)、分析補助が要る場合 S4(gpt-5.6-sol ultra)。1 Run 1 writer、worktree外への書き込み禁止(既存基盤)
- エージェントは git 操作(stage/commit/push)をしない。ゲート通過後に controller が候補commitを作る
- 予算超過・クォータ枯渇は既存 Budget/QuotaMonitor に従う(QUOTA_WAIT で休眠→自動復帰。self-dev-resume 基盤)

## 6. 検証(Gates)の要件
- GateRunner G1〜G4 を必須とし、S1 の自己申告テスト結果はマージ判定に使わない(既存原則)
- G1 の禁止path・secret検査は ProposalManifest の scope とも突合し、**scope外の変更があれば fail**
- GATES_FAILED 時の修正Runは1回のみ。同じゲートで2回失敗したら ABORTED+algedonic(pain)で S5 へ、人間向け通知イベントを残す
- S3★監査(AUDIT): 受入条件と diff・生ログ・予算実績を独立に突合し、所見を audit_report として FINAL_CONSORTIUM に提出

## 7. マージ準備(MERGE_READY)の要件
- 成果物: candidate branch(コミット済み)、PR説明文(Proposal要約+合議決定+gate report+監査所見+予算実績)、gate_report.json、audit_report
- push と PR 作成は v1 では人間(またはオーナーが事前に許可した場合のみ Fable)が実行
- MERGE_READY の一覧と成果物は WebUI から一望でき、ワンクリックで説明文をコピーできること

## 8. スケジューラ(ready-queue)の要件
- ROADMAP.md / openspec の未実装項目から実装候補を抽出し、依存関係・risk・変更path競合・クォータ残量を確認して**同時1件だけ** PROPOSED に進める
- 開始条件: 予測消費(1.3×見積)+ pool reserve ≤ 現在残量(selfhost-plan の式)。満たさない場合は次の reset まで待機
- 日次で self-development report(処理した Proposal、結果、消費、失敗と理由)を生成しイベント+ファイルで残す

## 9. 非目標(v1 でやらないこと)
- 自動 push / 自動 merge / protected path の自動変更
- 複数 Proposal の並列実装(直列のみ)
- 稼働中 Control Plane 自身の hot reload(マージ後の反映は人間の再起動)

## 10. 成功条件(v1 完了判定)
selfhost-plan のパイロット3件(docs のWSL一本化 / compose の loopback 限定 / CI拡張=protected扱いで人間事前承認)を、**提案から MERGE_READY まで無人**で3件連続完遂し、mainと稼働系が一度も壊れないこと。1件以上でクォータ枯渇→自動復帰を跨いで完遂すること。

## 11. Sol Ultra への詳細化依頼事項
1. 状態機械の正式定義(イベント名・スキーマ・永続化形式。既存 eventlog/schema.py への追加として)
2. ProposalManifest / audit_report / PR説明文のフィールド定義と、RunManifest への承継マッピング
3. Consortium への審議材料の組み立て方(プロンプト/コンテキスト構成。既存 consortium.py の参加者抽象にどう載せるか)
4. controller(ループ駆動)の実装配置: 常駐プロセスにするか、Web/CLIから駆動するイベントループにするか。再起動復元(self-dev-resume との統合)を含めて
5. 失敗マトリクス(各状態での異常系: クォータ枯渇・ゲート失敗・Consortium不成立・human timeout・worktree衝突)と遷移
6. API/CLI/WebUI 表面(最低限: proposal 作成/一覧/詳細、承認待ち一覧、MERGE_READY 一覧)
7. Luna 向け実装 Wave 分割(各Wave が独立にテスト可能で、既存402テストを壊さない順序)
