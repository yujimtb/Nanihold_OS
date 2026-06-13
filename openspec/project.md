# Project Context

> OpenSpec のプロジェクト共有コンテキスト。change/spec を読むエージェント・人間の前提を揃える。
> 詳細なロードマップは [docs/roadmap.md](../docs/roadmap.md)、アーキテクチャは
> [docs/architecture.md](../docs/architecture.md) を正とする。

## Purpose

組織を「観測 → ファジィに論理再構成 → 人間が確定 → 段階的に自動化」するための組織 OS を、
2027/3 のプロバイダー提供開始に向けて開発する。2つのリポジトリ/責任境界からなる。

- **LETHE (Rust)**: 観測基盤。生データの保持(Lake / Identity / FilteringGate / API)。生データの所在はここのみ。
- **Nanihold OS (Python)**: ランタイム。VSM(S1〜S5)に基づくタスク実行・会計・派生イベント。

## Tech Stack

- Nanihold OS: Python(`vsm` CLI、event-sourced な Event_Log = `events.jsonl`、LiteLLM 経由の LLM 呼び出し)
- LETHE: Rust(SQLite + blob、self-host binary、Slack/Notion adapter)
- フロントエンド: Web UI(Run 一覧 / LiveTopology / イベントストリーム)
- 実行環境: WSL + Docker Compose(`compose.yaml` の `app` サービス)
- テスト: pytest(Nanihold)/ cargo test + public-release-audit(LETHE)/ ゴールデンセット回帰(Q&A)

## Project Conventions

### Architecture Patterns

- **イベントソーシング**: Event_Log は append-only。スキーマ変更は「フィールド追加 + デフォルト値」のみ(破壊的変更禁止)。
- **派生イベントの provenance 必須**: Nanihold の Event_Log に入るのは FilteringGate 通過後の派生イベントだけで、由来 LETHE observation への参照を持つ。
- **書き込みは Tool 機構経由**: LETHE への書き戻しは Nanihold の ToolInvocation(EXTERNAL_WRITE)へ変換する(idempotency / 権限検証 / human review / 記録が自動で乗る)。
- **防御はデータ層に置く**: 情報の遮断はプロンプトではなく FilteringGate / scope / sandbox で構造的に行う。

### Testing Strategy

- DoD は「動いた」ではなく「壊し方を試して耐えた」基準で書く。
- 敵対テスト(顧客入力インジェクション、scope→sandbox 変換)を CI 常設にする。

### Git Workflow

- リポジトリ境界 = 人の境界(本人 = Nanihold / 友人 = LETHE)。PR は相互レビュー。

## Important Constraints

- **2026-06-24(水)**: SHIMOKITA COLLEGE チャットボット納期(確定・対外)。
- **2027-03**: 組織 OS プロバイダー提供開始(確定・対外)。
- 予算: LLM API 約 25,000 円/月(日次キャップ約 800 円)。ハードキャップを構造的に持つ。
- 個人情報感度の高いベータ先(教育・財団系)。同意・FilteringGate が前提。

## External Dependencies

- LLM プロバイダ(LiteLLM 経由、fallback 2系統)
- Slack / Notion(LETHE adapter)
- 外部死活監視(UptimeRobot 等の無料枠 or 友人側 AWS の最小構成)
