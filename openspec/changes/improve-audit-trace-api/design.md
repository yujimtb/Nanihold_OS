# Design: improve-audit-trace-api

## Context

監査トレース API は ACR-04 の読み取り面で、2 ルートを持つ(`vsm/web/app.py`):

```
GET /api/audit-traces/notifications/{notification_id}  -> AuditTraceService.trace_notification
GET /api/audit-traces/executions/{execution_id}        -> AuditTraceService.trace_execution
```

Interface は単一ワーカー(`uvicorn.run(..., workers=1)`, `vsm/cli.py:210`)で、両ルートは**同期 `def` ハンドラ**。Starlette は同期エンドポイントを anyio の有界スレッドプール(既定 40)で実行する。

`AuditTraceService.trace_*` は内部で `self._events()`(`vsm/audit_trace.py:87`)を呼び、Operational Ledger を **cursor 0 から末尾まで全走査**して対象 1 件に関連するイベントだけを線形フィルタする。台帳の実体は LETHE `/api/operational-events`、読みは `LetheOperationalLedger.page(after_cursor, limit)`(`vsm/lethe/client.py:226`)で、`limit` は `max_page_size`(実運用 500)で頭打ち。

- 台帳規模 ≈ 4.9 万イベント → 1 トレース = 約 98 回の逐次 HTTP 往復 = 3〜6 分。
- 応答本文はトレース dict を全構築後に一括返却(完了まで 0 バイト)。
- 同期ハンドラは切断でキャンセルされない(Starlette の仕様)。

## Problem 1: 並列で両方が無応答(work:audit-trace-concurrency)

観測: 2 件並行 → 両方 10 分で 0 バイトタイムアウト。直列なら成功。

推定メカニズム(実装 change で計測確認する前提の作業仮説):

1. 各トレースは 1 スレッドを 3〜6 分占有する長時間ブロッキング処理。2 件並行で LETHE への往復と接続プール(`LetheOperationalLedger` は単一 `httpx.Client` を共有)への負荷が倍化し、双方が 3〜6 分の下限を超えて 10 分の**クライアント**タイムアウトに達する。
2. さらに悪いことに、クライアントが 10 分で切断しても**同期ハンドラはキャンセルされず**、スレッドは全走査を継続する。呼び出しが繰り返されると滞留スレッドが積み上がり、スレッドプール枯渇 → 他エンドポイントも無応答という連鎖に至る。

つまり並列障害は「1 リクエストが重すぎる(Problem 2)」と「切断後も止まらない」の二重問題。Problem 2 の高速化だけでも大幅に緩和するが、切断非キャンセルは独立の欠陥として ATA-02 で塞ぐ。

### 対処方針

- **同期 `def` → 非同期 `async def` + 有界オフロード**: ハンドラを `async def` にし、ブロッキングな Ledger 読みを `anyio.to_thread.run_sync`(有界セマフォ付き)へ明示的に逃がす。`async def` はクライアント切断時に `CancelledError` を受け取れるため、切断の伝播点を持てる。
- **切断検知と協調キャンセル**: `Request.is_disconnected()` を走査ループの節目(ページ境界)でポーリングするか、`async` タスク側で切断を検知したら走査ワーカーへ協調停止フラグを送る。ワーカーは次のページ境界で `page()` の発行を止めて速やかに離脱する(ATA-02)。切断後に新規の LETHE 往復を発行しないことを不変条件とする。
- **同時実行の上限**: 監査トレースは高コスト読み取りなので、専用の並行度上限(セマフォ)を設けてスレッドプール全体の枯渇を防ぐ。上限超過は即座に決定的なビジー応答(例: 503 + Retry-After)を返し、無言でぶら下げない。上限値は設定値から解決しハードコードしない。

## Problem 2: 単一トレースが数分(work:audit-trace-performance)

根本原因は `_events()` の O(N) 全走査。対象イベントは台帳全体のごく一部(通知配送 1 + 昇格 0..1、または 名前割当 1 + receipt 1)なのに、それを見つけるために台帳を丸ごと舐めている。`OperationalLedger` プロトコル(`vsm/kernel/ledger.py`)が `page(after_cursor, limit)` と `stream(stream_id, after_version, limit)` しか持たず、**相関 id / event_type での絞り込みができない**ことが制約。

対象イベントの構造的手がかり(全走査を避ける鍵):

- `trace_notification`: `agent_notification_delivered` は payload.notification.notification_id == 対象、`agent_notification_promoted` は `stream_id == notification_id`。**昇格イベントは通知 id をストリーム id に持つ** → `stream()` で直接引ける。
- `trace_execution`: `agent_name_assigned` は payload.assignment.execution_id == 対象、`pilot_execution_receipt_recorded` は `stream_id == execution_id`。**receipt は execution id をストリーム id に持つ** → `stream()` で直接引ける。
- projection(`kernel.agent_notifications` / `kernel.executions` / `kernel.work_items`)は既にメモリ上にあり、`notification.promoted_work_item_id` や `execution.agent_name` 等の**確定値**を持つ。全走査はこれらと台帳の突合(検証)のために回っている面が大きい。

### 設計案の比較

本 change は「LETHE 側の相関 / type / keyset 索引が入る前提の設計」と「入らなくても成立する暫定設計」の両方を提示し、依存を明記する。

#### 案 A(LETHE 依存): `/api/operational-events` に相関 / type 索引を入れて絞り込み取得

skcollege_database 側の別 change で `/api/operational-events` に `event_type` / `correlation`(= notification_id / execution_id 相当)フィルタ、必要なら keyset を追加する。Nanihold 側は `OperationalLedger` プロトコルに絞り込み読み(例: `events_for(correlation_id, event_types)` あるいは `page` へのフィルタ引数)を足し、対象イベントだけを 1〜数往復で取得する。

- 長所: 台帳規模に対して真に O(対象件数)。最速。
- 短所: **LETHE 側 change の完了に依存**。索引が入るまで本 change の性能目標を満たせない(納期リスク)。プロトコル拡張は `InMemoryOperationalLedger` テスト double にも実装が要る。

#### 案 B(依存なし・暫定): 既存 `stream()` + ローカル索引 / キャッシュで全走査を廃止

LETHE の wire 契約を変えずに、現行プロトコルの範囲で全走査を消す。

- **B1(即効・最小)**: `delivered` / `receipt` / `promotion` の各対象は**対象 id をストリーム id に持つ**(上記の構造的手がかり)。これらは `LetheOperationalLedger.stream(stream_id=対象id, 0, limit)` で直接引ける。残る `agent_notification_delivered` / `agent_name_assigned` は payload 側に対象 id を持ちストリーム id が別 → これらだけは projection が持つ確定値(通知本体 / assignment)を真実として使い、台帳からは**ストリーム読みで取れる相方(昇格 / receipt)を突合検証**する。これにより全走査 `page(0..N)` を、数本の `stream()` 呼び出しに置換できる。
- **B2(堅牢化)**: Nanihold 側に **ローカルな派生索引**(`notification_id`/`execution_id` → 関連イベント cursor 群)を持つ。`persistent-search-index`(skcollege_database 側の先行事例)と同型で、台帳から増分・冪等に構築し、破損時は canonical(台帳)から決定的に再構築する。索引は真実ではなく派生(Affected Invariants 参照)。トレースは索引で cursor を引き、`stream()`/`page(cursor-1,1)`(cf. `vsm/pilot/host.py:26`)で当該イベントのみ取得。
- 長所: **LETHE 側 change を待たずに単独で出荷可能**。wire 契約不変。案 A が来たら B の読みを A の絞り込み読みに差し替えるだけ(段階移行)。
- 短所: B1 は「payload 側に id を持つイベント」を projection の確定値経由で検証する設計判断が要る(全走査時の "台帳から payload を突合" の一部を projection 突合へ移す)。B2 は索引の構築・破損・再構築の運用が増える。

### Decision(提案)

**主経路は案 B(依存なし)。LETHE 側索引は「待たない」。**

- 理由: 本 change の性能・並列目標は他リポジトリの change 完了に人質を取られるべきでない。対象イベントがストリーム id で直に引ける構造(B1)は既に存在するため、暫定設計は "劣化版" ではなく十分速い。
- **B1 を第一実装**(最小差分・全走査の即時廃止)、規模増や検証強化が要るなら **B2** を足す。
- 案 A(LETHE 相関 / type 索引)は**任意の高速化 fast-path** として扱う。skcollege_database 側 change が landing したら `OperationalLedger` の絞り込み読みへ差し替え、B の経路を fallback として残す。依存は soft(あれば速い、なくても目標達成)。
- ATA-04(改善前後一致)を回帰ゲートとして、B(および将来の A)の出力が全走査 canonical 実装とバイト等価であることを常に担保する。

## 並列性と性能の相互作用

Problem 2 の高速化(数分 → 数秒)は Problem 1 を大きく緩和する(スレッド占有が桁で短縮)。ただし切断非キャンセル(ATA-02)と並行度上限(ATA-01)は性能改善だけでは塞げない独立要件なので、両方を満たす。順序としては ATA-03(高速化)を先に入れると ATA-01/02 の負荷試験が安定する。

## Test / Acceptance 観点

- **再現テスト(ATA-01)**: 5 万イベント相当の台帳で 2 件の監査トレースを並行実行し、両方が成功して正しい結果を返す(直列でしか通らない現状の反証)。
- **切断テスト(ATA-02)**: トレース進行中にクライアントを切断し、サーバ側の LETHE 往復が有界回数で停止する(切断後の新規 `page()`/`stream()` 発行がゼロ)ことを観測する。切断を繰り返してもスレッドプールが枯渇しない。
- **性能テスト(ATA-03)**: 5 万イベントの合成台帳で単一トレース p95 を測り、数秒台の閾値(設定した SLO)を満たす。全走査ベースラインとの比を記録。
- **等価テスト(ATA-04)**: 同一入力で全走査実装(canonical)と新実装の出力(帰属・timeline・delivery.kind・verified)を突合し完全一致を確認。不変条件違反系(件数不一致・cursor 不連続・payload 不一致)も同一の `InvariantViolation` を送出。
- **契約テスト(ATA-05)**: 既存の API/サービステストが無改変で通る。wire 形状・ステータス・認可不変。
