# Change Proposal: improve-audit-trace-api

**Version:** 1.0
**Date:** 2026-07-22
**Status:** Proposed(仕様・設計のみ — 実装は別 change)
**Repository:** Nanihold_OS
**Type:** 既存 capability `audit-trace-api`(ACR-04 監査トレースの HTTP 面)の並列化・性能改善の設計起草
**Source:** 本番パイプラインに proposed 登録済みの WorkItem 2 件
- `work:audit-trace-concurrency`「監査トレース API の並列呼び出し安全化」
- `work:audit-trace-performance`「監査トレース API の性能改善」

> 注: 本 change は仕様・設計のみであり、実装しない。実装は承認後に別 change で行う。要件は上記 2 つの WorkItem に対応して 2 群に分ける(並列安全性: ATA-01/ATA-02、性能・正しさ・非破壊: ATA-03/ATA-04/ATA-05)。

---

## Why

監査トレース API(`GET /api/audit-traces/notifications/{id}`、`GET /api/audit-traces/executions/{id}`)は ACR-04 の「どのエージェントが何をやっているか」を後から辿れる唯一の読み取り面である。本番稼働で次の 2 つの障害が実測された。

**1. 並列呼び出しで相互ブロッキング(`work:audit-trace-concurrency`)**
`/api/audit-traces/*` を 2 件並行で呼ぶと、両方が無応答のままクライアントタイムアウト(10 分・0 バイト応答)に至る。直列に呼べば成功する。Interface は `uvicorn.run(..., workers=1)`(`vsm/cli.py:210`)で単一プロセス起動され、該当ルート(`vsm/web/app.py:441` `notification_audit_trace` / `:448` `execution_audit_trace`)は **同期 `def` ハンドラ**である。Starlette は同期ハンドラを anyio の有界スレッドプールで実行するため、1 リクエストあたり 1 スレッドを、後述の全走査が完了するまで(数分)占有する。加えて、同期ハンドラは**クライアント切断後もキャンセルされず処理を継続する**。切断後もスレッドと LETHE への往復を消費し続け、これが積み上がるとスレッドプールが枯渇して他エンドポイントまで無応答化する。応答本文はトレース dict を全構築し終えてから一括返却されるため、完了までは 0 バイトのままである。

**2. 単一トレースが本番相当データ量で数分(`work:audit-trace-performance`)**
`AuditTraceService._events()`(`vsm/audit_trace.py:87`)は操作台帳(Operational Ledger)を **cursor 0 から全走査**する。台帳の実体は LETHE `/api/operational-events` で、`LetheOperationalLedger.page`(`vsm/lethe/client.py:226`)は `after_cursor` + `limit`(最大 500、`max_page_size`)のカーソルページングしか提供しない。約 4.9 万イベントを 1 ページ 500 件で舐めると 1 呼び出しあたり約 98 往復・3〜6 分かかる。`trace_notification` / `trace_execution` は対象 1 件の関連イベント(通知配送・昇格、名前割当・receipt)を拾うためだけに全走査しており、`OperationalLedger` プロトコル(`vsm/kernel/ledger.py:10`)に相関 id / event_type での絞り込み手段がないことがボトルネックの根である。目標は単一トレース取得を数秒台にすることである。

## What Changes

- **ADDED:** ATA-01 並列トレース要求の相互非ブロッキング — 監査トレース要求が複数同時に到来しても相互にブロックせず、いずれも成功して応答を返す。実測の再現シナリオ(2 件並行 → 両方成功)を受け入れ条件に含む。
- **ADDED:** ATA-02 クライアント切断後のサーバ側処理の有界化 — クライアントが切断・タイムアウトした後に、サーバ側のトレース処理・LETHE 往復が無期限に残ってはならない。切断は速やかに検知して以降の走査・往復を打ち切り、スレッドプール枯渇の連鎖を防ぐ。
- **ADDED:** ATA-03 単一トレースの応答時間(本番相当データ量)— 台帳が本番相当(5 万イベント)でも、単一トレース取得(通知 / execution)が数秒台で完了する。全走査(cursor 0 からの O(N) 舐め)を廃し、対象に関連するイベントだけを取得する読み取りに置き換える。
- **ADDED:** ATA-04 トレース結果の正しさ(改善前後一致)— 改善後のトレース出力は、改善前の全走査実装と同一入力に対して同一の結果(同一の帰属・timeline・`verified` 判定)を返す。全走査が担保していた不変条件検証(配送/昇格の件数検査、cursor 連続性、payload とプロジェクションの一致等)を維持する。
- **ADDED:** ATA-05 既存契約・テストの非破壊 — 監査トレース API の wire 契約(レスポンス形状・ステータス・認可)と、`AuditTraceService` の公開関数(`trace_notification` / `trace_execution` / `trace_reply` / モジュール関数)の外形は維持し、既存テストを壊さない。
- **MODIFIED:** なし(`lethe-channel-bridge` の import / card-queue 契約、`reply-approval@1` 送信経路には触れない。ACR-04 の監査意味論は変更せず、その読み取り実装のみを差し替える)。

## Non-Goals

- ACR-04 監査トレースの**意味論**(何を検証し何を帰属として返すか)の変更。本 change は同じ結果をより速く・並列安全に返すことに徹する。
- `reply-draft@1` / `reply-approval@1` / `send-record@1` の supplemental 監査経路(`trace_reply`)の再設計。`trace_reply` は `kernel` 経由で `trace_execution` を呼ぶため、`trace_execution` の高速化の恩恵は受けるが、supplemental の突合ロジックは変更しない。
- LETHE 側 `/api/operational-events` への相関 / type / keyset 索引の実装そのもの(これは skcollege_database 側の別 change。本 change はその consumer 設計に徹し、索引が入らなくても成立する暫定設計を主経路とする — design.md 参照)。
- Interface 全体のワーカー多重化(`workers>1`)やプロセスモデルの変更。

## Affected Invariants

「インターフェースは複数、実体は一つ」を維持する。監査トレースは append-only の Operational Ledger を唯一の真実として**読み取るのみ**であり、読みながら監査イベントを追記しない(`vsm/audit_trace.py` 冒頭 docstring の read-only 原則)。本 change が導入しうるローカル索引 / キャッシュはすべて Ledger から決定的に再構築可能な**派生**であり、真実の実体は Ledger に一本化されたままとする(replay 可能性)。ATA-04 により、派生経由の結果は全走査(canonical)経由の結果と一致しなければならない。

## Rollout

本 change は設計仕様である。実装は本 change のスコープ外とし、承認後に別 change で行う。LETHE 側索引への依存は「待たない(暫定設計を主経路)」を提案する(design.md の Decision 参照)。
