# Nanihold OS ロードマップ

- 更新日: 2026-07-09
- 作成: Claude (自律作業セッション)。上流(node event model 世代)の実態調査・2026-06 のロードマップ議論・07-08〜09 の統合作業に基づく

## いまの姿 (2026-07-09 時点)

- Stafford Beer の VSM(Viable System Model)に基づく「AI 自動会社」ランタイム。2026-06 中旬の大規模リファクタで **Architecture / Role / Agent / Tool / Node の5層構成+ノードイベントモデル**に進化した
- **Webダッシュボード**(FastAPI + React/Vite)が実働: 日本語UIでタスク投入・ファイル添付・実行アーカイブ閲覧ができる(起動: `uvicorn vsm.web.app:app` + `cd frontend && npm run dev` → http://localhost:5173)
- ツールファサード 11 種(codex_run、search、spawn、human、escalation など)
- CLI(`vsm submit / status / tail / replay / runs`)は 2026-07-09 の統合で人間可読化済み
- テスト 334 件 + 1 skip(discord.py 未導入時)

## 2026-07-09 の統合作業(夜間UX改善の上流移植)

旧ベース(6/2 時点)で実装した UX 改善を、上流の新アーキテクチャへ手動移植した(機械マージは ASCII 制限の正反対決定があり不採用):
1. `vsm runs` 新設、`vsm status`/`replay` の人間可読化(イベントからの状態導出)、`vsm submit` の進捗表示、エラーの平文化
2. **実バグ修正: S5→S4→S5 フィードバックループによるテスト shutdown 40秒超ハング**(上流現行版で再現)を S4+S5 両停止で解消
3. **実バグ修正: pytest 並走時の一時ディレクトリ相互破壊**(固定 basetemp 共有が真因。決定論的に再現)→ PID 付きサブディレクトリ方式に変更(このマシンでは pytest 既定 Temp が権限エラーになるため repo 内 PID 方式を採用)
4. Web の artifact パスが Windows 区切り(\)で記録される不具合を / 固定に修正
5. dev / bot extras の分離(discord.py はテスト収集を skip)、smoke run を通常の `runs/<run_id>` に統一(CLI から一気通貫で確認可能)

移植しなかったもの: ASCII 制限まわりの旧文言(上流が日本語 description を解禁済みのため、持ち込むと退行)。旧ローカルコミットはブランチ `archive/overnight-ux-20260708` に保全。

## 次の一歩(短期)

1. **コスト可視化** — 上流の OpenSpec phase plans 自身が Phase 1 最優先(P0)に挙げているのに、CLI にも Web UI にもトークン消費・料金の表示が皆無。Run 単位・System 単位の消費を `vsm status` とダッシュボードに出すのが次の一手
2. **CLI Run と Web Run の統合** — 現状 CLI(`runs/<id>/`)と Web(`runs/web/<id>/runtime/<runtime_id>/`)が別ディレクトリ・別 run_id 体系で相互参照不能(ダッシュボードの「0 runs」表示は CLI Run が見えていないため)。片方に寄せるか相互リンクする
3. **ランタイムのシャットダウン品質** — 調査で発見した6システム共通の実バグ2件(キャンセル時の孤児タスクリーク、シャットダウン競合時に最大2メッセージ消失)の修正。テストは直したがランタイム本体は未修正
4. Web ダッシュボードの起動手順を README に一本化(uvicorn + vite の2プロセス、API 既定 http://localhost:8000、`VITE_API_BASE_URL` で変更可)

## 中期(LETHE / Eos との接続)

- S4(環境スキャン)の情報源として LETHE(個人データレイク)を使う — 「会社」が持ち主の実データを観測できるようになる
- Run 間の長期記憶を独自実装せず、LETHE への observation / supplemental 書き込みで代替する
- 人間承認(human ツール)を Eos の reply cards / 承認フローと共通化する

## 長期の方向性

「AI 自動会社」— 自律的に環境を観測し(S4)、方針を保ち(S5)、資源を配分し(S3)、実務を行う(S1)組織体。MVP スコープとして保留していた項目(FSX 数値最適化、公共性測定、共有剰余配分、再帰的サブ VSM)は、コスト可視化と Run 統合で運用が安定してから。

## やらないこと(現時点)

- S1 の無制限な実行能力(codex_run ファサードの承認・サンドボックス設計を超える拡張)
- ASCII 制限の復活(日本語ファーストで進む)
