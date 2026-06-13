# Phase 0: チャットボット納品(6/13–6/24)

## Why

SHIMOKITA COLLEGE 向けチャットボットの納期 6/24(水)は確定・対外制約であり、検収通過と
サブスク開始がロードマップ全体の生存原資(Track A)の前提になる。本 change は Phase 0 の
スコープ — LETHE の安全な読取基盤(認証・FilteringGate・定期同期)と、その上に乗る
意図的に薄い Q&A サービス — を OpenSpec の要件として確定する。

主戦場は LETHE + Q&A サービスで、Nanihold ランタイムには触らない。方針は
**「スコープ削減 > 納期延長」**。落とすのは回答カテゴリ、守るのは 6/24。

## What Changes

- **lethe-api-auth**(L-1): 全 `/api/*` `/admin/*` を scope 付き Bearer トークン必須にする
- **lethe-filtering-gate**(L-2): 回答生成に使う全取得経路を FilteringGate 通過必須にし、
  Visibility/consent 外データを **データ層で構造的に遮断**(プロンプトで隠さない)
- **lethe-sync**(L-3): `POST /admin/sync` を専用トークンで定期実行し、失敗を通知に接続
- **qa-service**(Q-1〜Q-5): Slack 経由の質問に対する retrieval + 出典付き回答生成、
  質問文の隔離、拒否・エスカレーション、回答ログと運営者レビュー、日次コストキャップ、
  検収・回帰のためのゴールデン質問セット

### スコープ外(本 change に含めない)

Discord adapter / Notion write-back 拡張 / 埋め込み検索 / Nanihold 統合 /
マルチテナント。いずれも v1 検収に不要で、追加要望は v1.1(7月以降)として別途記録する。

## Impact

- Affected specs (new): `lethe-api-auth`, `lethe-filtering-gate`, `lethe-sync`, `qa-service`
- Affected code: LETHE(Rust)の API / FilteringGate / sync、新規 Q&A サービス
- 対外・事業: B-1 仕様合意文書(6/16 凍結)、6/24 検収立ち会い、サブスク開始
- リスク: R1(納期遅延)/ R2(誤答事故)— 対応は design.md と §12 リスク登録簿
