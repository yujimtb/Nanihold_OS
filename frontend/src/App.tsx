import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  Bot,
  Boxes,
  Cable,
  CheckCircle2,
  ChevronRight,
  CircleDollarSign,
  GitMerge,
  History,
  KeyRound,
  MessageSquareText,
  RefreshCw,
  Route,
  Search,
  Send,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
  Waypoints,
} from "lucide-react";
import { ApiClient, ApiError, browserDeviceId } from "./api";
import type {
  ActivationState,
  ActivationStatus,
  Commitment,
  Conversation,
  ConversationActionReceipt,
  DataSpace,
  Decision,
  EventItem,
  Execution,
  Message,
  ModelCandidate,
  Node,
  PilotSession,
  ReorientationAssessment,
  SurfaceBinding,
  WorkEdge,
  WorkItem,
} from "./types";

type View = "command" | "conversation" | "ledger" | "routing" | "audit";
type AuthState = "checking" | "authenticated" | "unauthenticated";

type PilotUsage = {
  input_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
  output_tokens: number;
  cost_usd: number;
};

type PilotHost = {
  identity: { pilot_host_id: string };
  state: string;
  quota_remaining_percent?: number;
  quota_reset_at?: string;
};

type Snapshot = {
  activation: ActivationStatus;
  spaces: DataSpace[];
  nodes: {
    items: Node[];
    capability_grants: unknown[];
    reference_grants: unknown[];
  };
  work: { items: WorkItem[]; edges: WorkEdge[] };
  executions: {
    items: Execution[];
    effect_leases: Array<{
      lease_id: string;
      effect_kind: string;
      state: string;
    }>;
    budget_reservations: Array<{
      reservation_id: string;
      amount: number;
      currency: string;
      token_limit: number;
    }>;
  };
  events: { events: EventItem[]; next_cursor: number };
  conversations: {
    items: Conversation[];
    surface_bindings: SurfaceBinding[];
    pilot_sessions: PilotSession[];
    messages: Record<string, Message[]>;
    commitments: Commitment[];
    decisions: Decision[];
    node_memories: unknown[];
  };
  hosts: PilotHost[];
  models: {
    candidates: ModelCandidate[];
    verified_outcomes: unknown[];
    evidence_cursor: number;
  };
  routes: {
    items: Array<{
      snapshot_id: string;
      production_objective: string;
      state: string;
    }>;
    scores: Record<
      string,
      Array<{
        candidate_key: string;
        reliability: number;
        expected_tokens: number;
        expected_cost: number;
        ranks: Record<string, number>;
      }>
    >;
  };
  lab: {
    observations: Array<{
      observation_id: string;
      total_input_tokens: number;
      incident_kinds: string[];
    }>;
  };
};

const ACTIVATION_STATES: ActivationState[] = [
  "UNCOMMISSIONED",
  "HISTORY_IMPORTED",
  "REORIENTATION_ONLY",
  "AWAITING_OWNER_CONFIRMATION",
  "ACTIVE",
];

const ACTIVATION_COPY: Record<
  ActivationState,
  { title: string; detail: string }
> = {
  UNCOMMISSIONED: {
    title: "履歴の取込を待っています",
    detail:
      "Interface Pilotはまだ起動していません。Claude、Codex、Intercom、LETHEの取込receiptが揃うまでExecutionは開始されません。",
  },
  HISTORY_IMPORTED: {
    title: "履歴の検証が完了しました",
    detail:
      "全履歴のdigestとcursorは一致しています。Interface Pilotを履歴読解専用モードで開始できます。",
  },
  REORIENTATION_ONLY: {
    title: "Interface Pilotが過去を読み直しています",
    detail:
      "Personal Lakeから必要な根拠を追加取得中です。この間、WorkItem実行と外部副作用は禁止されています。",
  },
  AWAITING_OWNER_CONFIRMATION: {
    title: "Interface Pilotが追いつきました",
    detail:
      "理解した状況を確認してください。訂正は過去を書き換えず、新しいDecisionとして保存されます。",
  },
  ACTIVE: {
    title: "Interface Pilotは活動中です",
    detail:
      "確認済みの状況を基に、Naniholdの委任、routing、Effect、監査を使って作業しています。",
  },
};

function short(value: string, keep = 12) {
  return value.length > keep ? `${value.slice(0, keep)}…` : value;
}

function StatePill({ value }: { value: string }) {
  return (
    <span className={`pill state-${value.toLowerCase()}`}>
      {value.replaceAll("_", " ")}
    </span>
  );
}

function pilotUsage(value: unknown): PilotUsage | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  const keys: Array<keyof PilotUsage> = [
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "cost_usd",
  ];
  if (!keys.every((key) => typeof item[key] === "number")) return null;
  return item as PilotUsage;
}

function AssessmentSection({
  title,
  items,
  emptyText,
}: {
  title: string;
  items: string[];
  emptyText: string;
}) {
  return (
    <section className="assessment-section">
      <h3>{title}</h3>
      {items.length ? (
        <ul>
          {items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : (
        <p className="empty">{emptyText}</p>
      )}
    </section>
  );
}

function ReorientationBrief({
  assessment,
  commitments,
  work,
}: {
  assessment: ReorientationAssessment;
  commitments: Commitment[];
  work: WorkItem[];
}) {
  const commitmentText = assessment.open_commitment_ids.map((id) => {
    const found = commitments.find((item) => item.commitment_id === id);
    return found ? `${found.statement} (${short(id)})` : id;
  });
  const resumeText = assessment.resume_work_item_ids.map((id) => {
    const found = work.find((item) => item.work_item_id === id);
    return found ? `${found.title} (${short(id)})` : id;
  });
  return (
    <div className="assessment-grid">
      <section className="assessment-understanding">
        <p className="eyebrow">INTERFACE UNDERSTANDING</p>
        <p>{assessment.understanding}</p>
        <div className="coverage">
          <span>{assessment.covered_session_count} sessions covered</span>
          <span>history #{assessment.history_cursor}</span>
          <span>current state #{assessment.current_state_cursor}</span>
        </div>
      </section>
      <AssessmentSection
        title="進行中のミッション"
        items={assessment.active_missions}
        emptyText="進行中と判断したミッションはありません。"
      />
      <AssessmentSection
        title="有効な決定・制約"
        items={assessment.decisions_and_constraints}
        emptyText="追加の制約はありません。"
      />
      <AssessmentSection
        title="未履行の約束"
        items={commitmentText}
        emptyText="未履行の約束はありません。"
      />
      <AssessmentSection
        title="不明点・矛盾"
        items={assessment.unknowns}
        emptyText="重大な不明点は検出されていません。"
      />
      <AssessmentSection
        title="再開候補"
        items={resumeText}
        emptyText="再開候補はありません。"
      />
      <details className="evidence">
        <summary>
          根拠を確認
          <span>{assessment.citations.length} citations</span>
        </summary>
        <div>
          {assessment.citations.map((citation, index) => (
            <article key={`${citation.claim_ref}:${citation.evidence_ref}:${index}`}>
              <span>{citation.claim_ref}</span>
              <code>{citation.evidence_ref}</code>
            </article>
          ))}
          {!assessment.citations.length && (
            <p className="empty">引用可能な根拠がありません。</p>
          )}
        </div>
      </details>
    </div>
  );
}

function ActivationPanel({
  snapshot,
  busy,
  correction,
  onCorrection,
  onStart,
  onApprove,
  onRevise,
}: {
  snapshot: Snapshot;
  busy: boolean;
  correction: string;
  onCorrection: (value: string) => void;
  onStart: () => void;
  onApprove: () => void;
  onRevise: () => void;
}) {
  const activation = snapshot.activation;
  const copy = ACTIVATION_COPY[activation.state];
  const currentIndex = ACTIVATION_STATES.indexOf(activation.state);
  return (
    <section
      className={`activation-card activation-${activation.state.toLowerCase()}`}
      aria-labelledby="activation-title"
    >
      <div className="activation-head">
        <div className="activation-icon">
          {activation.state === "ACTIVE" ? (
            <Sparkles size={21} />
          ) : activation.state === "AWAITING_OWNER_CONFIRMATION" ? (
            <ShieldCheck size={21} />
          ) : (
            <History size={21} />
          )}
        </div>
        <div>
          <p className="eyebrow">INTERFACE ACTIVATION</p>
          <h2 id="activation-title">{copy.title}</h2>
          <p>{copy.detail}</p>
        </div>
        <StatePill value={activation.state} />
      </div>
      <ol className="activation-steps" aria-label="Interface起動段階">
        {ACTIVATION_STATES.map((state, index) => (
          <li
            key={state}
            className={
              index < currentIndex
                ? "complete"
                : index === currentIndex
                  ? "current"
                  : ""
            }
            aria-current={index === currentIndex ? "step" : undefined}
          >
            <span>{index < currentIndex ? <CheckCircle2 size={13} /> : index + 1}</span>
            <small>{state.replaceAll("_", " ")}</small>
          </li>
        ))}
      </ol>
      {activation.import_receipt && (
        <div className="activation-facts">
          <span>
            {activation.import_receipt.sources.reduce(
              (total, source) => total + source.record_count,
              0,
            ).toLocaleString()}{" "}
            records imported
          </span>
          <span>{activation.import_receipt.sources.length} source receipts</span>
          <span>{activation.reorientation_pilot_calls} reorientation calls</span>
          <span>
            {activation.reorientation_input_tokens.toLocaleString()} input /{" "}
            {activation.reorientation_output_tokens.toLocaleString()} output
          </span>
        </div>
      )}
      {activation.state === "HISTORY_IMPORTED" && (
        <button className="primary activation-action" onClick={onStart} disabled={busy}>
          <History size={16} />
          {busy ? "開始中…" : "Interface Pilotに履歴読解を開始させる"}
        </button>
      )}
      {activation.state === "REORIENTATION_ONLY" && (
        activation.reorientation_error ? (
          <div className="reading-status error-banner" role="alert">
            <TriangleAlert size={16} />
            <span>
              履歴読解は安全に停止しました（{activation.reorientation_error}）。
              ExecutionとEffectは開始されていません。
            </span>
            <button className="primary" onClick={onStart} disabled={busy}>
              <RefreshCw size={16} />
              {busy ? "再開中…" : "履歴読解を再開"}
            </button>
          </div>
        ) : activation.reorientation_attempt_in_progress ? (
          <div className="reading-status" role="status" aria-live="polite">
            <RefreshCw className="spin" size={16} />
            Interface Pilotが索引とraw履歴を照合しています。画面更新はモデルを呼びません。
          </div>
        ) : activation.pending_reorientation_revision_reason ? (
          <div className="reading-status" role="status">
            <History size={16} />
            <span>
              Assessmentの再評価理由を記録済みです。ExecutionとEffectは開始されていません。
            </span>
            <button className="primary" onClick={onStart} disabled={busy}>
              <RefreshCw size={16} />
              {busy ? "再評価中…" : "再評価を開始"}
            </button>
          </div>
        ) : (
          <div className="reading-status error-banner" role="alert">
            <TriangleAlert size={16} />
            再オリエンテーション状態に開始理由がありません。運用監査が必要です。
          </div>
        )
      )}
      {activation.assessment && (
        <ReorientationBrief
          assessment={activation.assessment}
          commitments={snapshot.conversations.commitments}
          work={snapshot.work.items}
        />
      )}
      {activation.state === "AWAITING_OWNER_CONFIRMATION" &&
        activation.assessment && (
          activation.assessment.resume_work_item_ids.length === 0 ? (
            <div className="reading-status error-banner" role="alert">
              <TriangleAlert size={16} />
              <span>
                実在する未完WorkItemが再開候補に含まれていません。この理解は承認できません。
              </span>
              <button className="primary" onClick={onRevise} disabled={busy}>
                <RefreshCw size={16} />
                {busy ? "再評価を開始中…" : "未完WorkItemを含めて再評価"}
              </button>
            </div>
          ) : (
            <div className="approval-box">
              <label htmlFor="owner-correction">
                訂正があれば1行ずつ入力してください
              </label>
              <textarea
                id="owner-correction"
                value={correction}
                onChange={(event) => onCorrection(event.target.value)}
                placeholder={"例: 優先順位はAよりBが先です\n例: この約束はすでに完了しています"}
              />
              <div>
                <small>
                  空欄のまま承認できます。承認後、履歴から選ばれた実在WorkItemを再開します。
                </small>
                <button className="primary" onClick={onApprove} disabled={busy}>
                  <ShieldCheck size={16} />
                  {busy ? "確認を保存中…" : "理解を確認してInterface Pilotを起動"}
                </button>
              </div>
            </div>
          )
        )}
    </section>
  );
}

export default function App() {
  const deviceId = useMemo(() => browserDeviceId(), []);
  const client = useMemo(() => new ApiClient(deviceId), [deviceId]);
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [bootstrapCode, setBootstrapCode] = useState(
    () => new URLSearchParams(window.location.search).get("code") ?? "",
  );
  const [view, setView] = useState<View>("command");
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyAction, setBusyAction] = useState(false);
  const [filter, setFilter] = useState("");
  const [selectedConversation, setSelectedConversation] = useState("");
  const [message, setMessage] = useState("");
  const [correction, setCorrection] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [
        activation,
        spaces,
        nodes,
        work,
        executions,
        events,
        conversations,
        hosts,
        models,
        routes,
        lab,
      ] = await Promise.all([
        client.get<ActivationStatus>("/api/activation/status"),
        client.get<Snapshot["spaces"]>("/api/data-spaces"),
        client.get<Snapshot["nodes"]>("/api/nodes"),
        client.get<Snapshot["work"]>("/api/work-items"),
        client.get<Snapshot["executions"]>("/api/executions"),
        client.get<Snapshot["events"]>("/api/events?after_cursor=0&limit=250"),
        client.get<Snapshot["conversations"]>("/api/conversations"),
        client.get<Snapshot["hosts"]>("/api/pilot-hosts"),
        client.get<Snapshot["models"]>("/api/model-registry"),
        client.get<Snapshot["routes"]>("/api/route-snapshots"),
        client.get<Snapshot["lab"]>("/api/token-lab"),
      ]);
      setSnapshot({
        activation,
        spaces,
        nodes,
        work,
        executions,
        events,
        conversations,
        hosts,
        models,
        routes,
        lab,
      });
      setSelectedConversation((existing) =>
        existing || conversations.items[0]?.conversation_id || "",
      );
      setAuthState("authenticated");
      setError(null);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        setAuthState("unauthenticated");
        setSnapshot(null);
        setError(null);
      } else {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      setLoading(false);
    }
  }, [client]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (authState !== "authenticated") return;
    const timer = window.setInterval(() => void refresh(), 15_000);
    return () => window.clearInterval(timer);
  }, [authState, refresh]);

  async function exchangeBootstrap() {
    if (!bootstrapCode.trim()) return;
    setBusyAction(true);
    try {
      await client.post<{ device_id: string; expires_at: string }>(
        "/api/owner-bootstrap/exchange",
        {
          code: bootstrapCode.trim(),
          device_id: deviceId,
          idempotency_key: `web-bootstrap:${crypto.randomUUID()}`,
        },
      );
      window.history.replaceState({}, document.title, window.location.pathname);
      setBootstrapCode("");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction(false);
    }
  }

  function ownerId(): string {
    const id = snapshot?.spaces[0]?.owner_id;
    if (!id) throw new Error("Owner DataSpace is unavailable");
    return id;
  }

  async function startReorientation() {
    setBusyAction(true);
    try {
      await client.post("/api/reorientation/start", {
        actor_id: ownerId(),
        idempotency_key: `web:reorientation-start:${crypto.randomUUID()}`,
      });
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction(false);
    }
  }

  async function approveReorientation() {
    const assessment = snapshot?.activation.assessment;
    if (!assessment) return;
    setBusyAction(true);
    try {
      const corrections = correction
        .split("\n")
        .map((item) => item.trim())
        .filter(Boolean);
      await client.post("/api/reorientation/approval", {
        assessment_id: assessment.assessment_id,
        conversation_id: assessment.conversation_id,
        corrections,
        actor_id: ownerId(),
        idempotency_key: `web:reorientation-approval:${crypto.randomUUID()}`,
      });
      setCorrection("");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction(false);
    }
  }

  async function reviseReorientation() {
    setBusyAction(true);
    try {
      await client.post("/api/reorientation/revision", {
        reason_code: "missing_resume_work_item",
        requested_by: "owner",
        actor_id: ownerId(),
        idempotency_key: `web:reorientation-revision:${crypto.randomUUID()}`,
      });
      await client.post("/api/reorientation/start", {
        actor_id: ownerId(),
        idempotency_key: `web:reorientation-revision-start:${crypto.randomUUID()}`,
      });
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction(false);
    }
  }

  async function sendMessage() {
    if (!selectedConversation || !message.trim()) return;
    const sourceMessageId = `web-message:${crypto.randomUUID()}`;
    const actionId = `action:${crypto.randomUUID()}`;
    setBusyAction(true);
    try {
      const receipt = await client.post<ConversationActionReceipt>(
        `/api/conversations/${selectedConversation}/actions`,
        {
          action_id: actionId,
          idempotency_key: `web:${actionId}`,
          kind: "owner_message",
          text: message,
          source: {
            surface: "web",
            source_session_id: deviceId,
            source_message_id: sourceMessageId,
            author_id: ownerId(),
            channel_id: selectedConversation,
            occurred_at: new Date().toISOString(),
          },
        },
      );
      if (receipt.status === "failed") {
        throw new Error(receipt.error ?? "Owner action failed");
      }
      setMessage("");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction(false);
    }
  }

  async function intervene(workItemId: string) {
    const reason = window.prompt("停止理由を入力してください");
    if (!reason?.trim()) return;
    setBusyAction(true);
    try {
      await client.post(`/api/work-items/${workItemId}/interventions`, {
        actor_id: ownerId(),
        reason,
        idempotency_key: `web:${crypto.randomUUID()}`,
      });
      await refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusyAction(false);
    }
  }

  if (authState !== "authenticated" || !snapshot) {
    return (
      <main className="login-shell">
        <section className="login-card" aria-labelledby="login-title">
          <div className="brand-mark">
            {authState === "checking" ? (
              <RefreshCw className="spin" size={23} />
            ) : (
              <KeyRound size={23} />
            )}
          </div>
          <p className="eyebrow">OWNER DEVICE</p>
          <h1 id="login-title">
            {authState === "checking"
              ? "この端末の認証を確認しています"
              : "一度だけ、この端末をInterface Pilotにつなぎます"}
          </h1>
          {authState === "checking" ? (
            <p className="muted" role="status">
              HttpOnly owner sessionを確認中です。秘密情報の貼り付けは不要です。
            </p>
          ) : (
            <>
              <p className="muted">
                Naniholdが発行した短時間有効なowner bootstrap linkを開くか、codeを入力してください。認証情報はHttpOnly cookieとして保存されます。
              </p>
              <label htmlFor="bootstrap-code">
                Owner bootstrap code
              </label>
              <input
                id="bootstrap-code"
                type="password"
                autoComplete="one-time-code"
                value={bootstrapCode}
                onChange={(event) => setBootstrapCode(event.target.value)}
                onKeyDown={(event) =>
                  event.key === "Enter" && void exchangeBootstrap()
                }
              />
              <p className="device-id">
                この端末: <code>{deviceId}</code>
              </p>
              {error && (
                <div className="error-banner" role="alert">
                  {error}
                </div>
              )}
              <button
                className="primary"
                onClick={() => void exchangeBootstrap()}
                disabled={busyAction || !bootstrapCode.trim()}
              >
                <KeyRound size={16} />
                {busyAction ? "認証中…" : "この端末を認証"}
              </button>
            </>
          )}
        </section>
      </main>
    );
  }

  const activeWork = snapshot.work.items.filter((item) =>
    ["ready", "active", "paused", "blocked"].includes(item.state),
  );
  const waitingWork = activeWork.filter((item) =>
    ["paused", "blocked"].includes(item.state),
  );
  const visibleEvents = snapshot.events.events
    .filter((item) =>
      `${item.event.event_type} ${item.event.stream_id}`
        .toLowerCase()
        .includes(filter.toLowerCase()),
    )
    .slice()
    .reverse();
  const messages = snapshot.conversations.messages[selectedConversation] ?? [];
  const route = snapshot.routes.items.find((item) => item.state === "published");
  const scores = route ? snapshot.routes.scores[route.snapshot_id] ?? [] : [];
  const selected = scores.find(
    (item) =>
      item.ranks[route?.production_objective ?? "quality_max"] === 1,
  );
  const interfaceModel =
    snapshot.models.candidates.find(
      (model) => model.key === selected?.candidate_key,
    ) ?? snapshot.models.candidates[0];
  const latestPilotUsage = pilotUsage(
    snapshot.events.events
      .slice()
      .reverse()
      .find(
        (item) =>
          item.event.event_type === "interface_response_recorded" &&
          item.event.stream_id === selectedConversation,
      )?.event.payload.pilot_usage,
  );
  const quotaHost = snapshot.hosts.find(
    (host) => typeof host.quota_remaining_percent === "number",
  );
  const evidenceCount =
    snapshot.work.items.filter((item) => item.completion_evidence).length +
    (snapshot.activation.assessment?.citations.length ?? 0);

  return (
    <div className="app-shell">
      <aside>
        <div className="brand">
          <div className="brand-mark">
            <Waypoints size={20} />
          </div>
          <div>
            <strong>Nanihold</strong>
            <span>persistent interface</span>
          </div>
        </div>
        <nav aria-label="主要画面">
          {(
            [
              ["command", Boxes, "現在"],
              ["conversation", MessageSquareText, "Interface"],
              ["ledger", Activity, "根拠"],
              ["routing", Route, "Routing"],
              ["audit", ShieldCheck, "監査"],
            ] as const
          ).map(([key, Icon, label]) => (
            <button
              key={key}
              className={view === key ? "active" : ""}
              onClick={() => setView(key)}
              aria-current={view === key ? "page" : undefined}
            >
              <Icon size={17} />
              {label}
            </button>
          ))}
        </nav>
        <div className="connection">
          <span className={error ? "dot danger" : "dot"} />
          <div>
            <strong>{error ? "確認が必要" : "Personal Lake接続中"}</strong>
            <span>{snapshot.spaces[0]?.data_space_id}</span>
          </div>
        </div>
      </aside>

      <main>
        <header>
          <div>
            <p className="eyebrow">OWNER INTERFACE NODE</p>
            <h1>
              {view === "command"
                ? "Interfaceと仕事の現在地"
                : view === "conversation"
                  ? "Interfaceとの会話"
                  : view}
            </h1>
          </div>
          <div className="header-actions">
            <label className="search">
              <Search size={15} />
              <span className="sr-only">Eventを絞り込む</span>
              <input
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                placeholder="根拠を検索"
              />
            </label>
            <button
              className="icon-button"
              onClick={() => void refresh()}
              aria-label="最新状態へ更新"
            >
              <RefreshCw size={17} className={loading ? "spin" : ""} />
            </button>
          </div>
        </header>

        {error && (
          <div className="error-banner" role="alert">
            <TriangleAlert size={16} />
            {error}
          </div>
        )}

        <ActivationPanel
          snapshot={snapshot}
          busy={busyAction}
          correction={correction}
          onCorrection={setCorrection}
          onStart={() => void startReorientation()}
          onApprove={() => void approveReorientation()}
          onRevise={() => void reviseReorientation()}
        />

        {view === "command" && (
          <>
            <section className="metrics operational-metrics">
              <article>
                <span>現在</span>
                <strong>{activeWork.length - waitingWork.length}</strong>
                <small>
                  {snapshot.executions.items.filter(
                    (item) => item.state === "active",
                  ).length}{" "}
                  executions active
                </small>
              </article>
              <article>
                <span>待っています</span>
                <strong>{waitingWork.length}</strong>
                <small>
                  {snapshot.activation.state === "AWAITING_OWNER_CONFIRMATION"
                    ? "owner confirmationを含む"
                    : "paused / blocked"}
                </small>
              </article>
              <article>
                <span>委任</span>
                <strong>
                  {
                    new Set(
                      activeWork.map((item) => item.delegated_to_node_id),
                    ).size
                  }
                </strong>
                <small>
                  {snapshot.hosts.filter((item) => item.state === "connected").length}{" "}
                  PilotHosts connected
                </small>
              </article>
              <article>
                <span>費用・quota</span>
                <strong>
                  {latestPilotUsage
                    ? `$${latestPilotUsage.cost_usd.toFixed(3)}`
                    : "—"}
                </strong>
                <small>
                  {quotaHost?.quota_remaining_percent !== undefined
                    ? `${quotaHost.quota_remaining_percent}% remaining`
                    : "quota telemetry待ち"}
                </small>
              </article>
              <article>
                <span>根拠</span>
                <strong>{evidenceCount}</strong>
                <small>citations + completion evidence</small>
              </article>
            </section>
            <div className="grid two-one">
              <section className="panel">
                <div className="panel-title">
                  <div>
                    <p className="eyebrow">WORK GRAPH</p>
                    <h2>誰が実行し、誰が統合するか</h2>
                  </div>
                  <GitMerge size={19} />
                </div>
                <div className="work-list">
                  {activeWork.map((work) => (
                    <article key={work.work_item_id}>
                      <div className="work-head">
                        <div>
                          <strong>{work.title}</strong>
                          <span>{short(work.work_item_id, 24)}</span>
                        </div>
                        <div className="work-actions">
                          <StatePill value={work.state} />
                          {["ready", "active", "blocked"].includes(work.state) && (
                            <button
                              onClick={() => void intervene(work.work_item_id)}
                              disabled={busyAction}
                            >
                              停止
                            </button>
                          )}
                        </div>
                      </div>
                      <div className="work-route">
                        <span>{short(work.delegated_to_node_id)}</span>
                        <ChevronRight size={12} />
                        <span>{short(work.integration_owner_node_id)}</span>
                      </div>
                      <div className="criteria">
                        {work.acceptance_criteria.map((criterion) => (
                          <span key={criterion}>
                            <CheckCircle2 size={13} />
                            {criterion}
                          </span>
                        ))}
                      </div>
                      {work.state === "paused" && (
                        <p className="waiting-reason">Pilotまたは介入解除待ち</p>
                      )}
                    </article>
                  ))}
                  {!activeWork.length && (
                    <p className="empty">現在動いているWorkItemはありません。</p>
                  )}
                </div>
              </section>
              <section className="panel">
                <div className="panel-title">
                  <div>
                    <p className="eyebrow">NODE TREE</p>
                    <h2>参加している主体</h2>
                  </div>
                  <Waypoints size={19} />
                </div>
                <div className="node-list">
                  {snapshot.nodes.items.map((node) => (
                    <article key={node.node_id}>
                      <div className="node-icon">
                        {node.kind === "interface" ? (
                          <Sparkles size={17} />
                        ) : (
                          <Boxes size={17} />
                        )}
                      </div>
                      <div>
                        <strong>{node.name}</strong>
                        <span>
                          {node.kind} · {node.status}
                        </span>
                        <div className="functions">
                          {node.resident_functions.map((fn) => (
                            <i key={fn}>{fn}</i>
                          ))}
                        </div>
                      </div>
                    </article>
                  ))}
                </div>
              </section>
            </div>
          </>
        )}

        {view === "conversation" && (
          <section className="conversation-layout">
            <div className="conversation-list panel">
              <p className="eyebrow">CANONICAL CONVERSATION</p>
              {snapshot.conversations.items.map((item) => {
                const surfaces = snapshot.conversations.surface_bindings.filter(
                  (binding) => binding.conversation_id === item.conversation_id,
                );
                const sessions = snapshot.conversations.pilot_sessions.filter(
                  (session) => session.conversation_id === item.conversation_id,
                );
                return (
                  <button
                    key={item.conversation_id}
                    className={
                      selectedConversation === item.conversation_id
                        ? "selected"
                        : ""
                    }
                    onClick={() =>
                      setSelectedConversation(item.conversation_id)
                    }
                  >
                    <MessageSquareText size={16} />
                    <div>
                      <strong>{item.title}</strong>
                      <span>
                        {surfaces.map((surface) => surface.surface).join(" · ") ||
                          "Web"}
                        {" · "}
                        {sessions.length} Pilot sessions
                      </span>
                    </div>
                  </button>
                );
              })}
            </div>
            <div className="chat panel">
              <div className="chat-head">
                <div>
                  <p className="eyebrow">INTERFACE PILOT</p>
                  <h2>
                    {interfaceModel
                      ? `${interfaceModel.model_snapshot} · ${interfaceModel.effort}`
                      : "routing evidence待ち"}
                  </h2>
                </div>
                <span className="model-note">
                  {latestPilotUsage
                    ? `${latestPilotUsage.input_tokens.toLocaleString()} input · ${latestPilotUsage.cache_read_input_tokens.toLocaleString()} cache read · ${latestPilotUsage.output_tokens.toLocaleString()} output · $${latestPilotUsage.cost_usd.toFixed(4)}`
                    : "最大1 Interface call / owner turn"}
                </span>
              </div>
              <div className="messages" aria-live="polite">
                {messages.map((item) => (
                  <article key={item.message_id} className={item.role}>
                    <span>{item.role}</span>
                    <p>{item.display_text}</p>
                  </article>
                ))}
                {!messages.length && (
                  <p className="empty">このConversationにはまだ表示メッセージがありません。</p>
                )}
              </div>
              <div className="composer">
                <label className="sr-only" htmlFor="owner-message">
                  Interfaceへのメッセージ
                </label>
                <textarea
                  id="owner-message"
                  value={message}
                  onChange={(event) => setMessage(event.target.value)}
                  placeholder="短い訂正だけでも作戦を継続できます…"
                  disabled={snapshot.activation.state !== "ACTIVE"}
                />
                <button
                  className="primary"
                  onClick={() => void sendMessage()}
                  disabled={
                    busyAction ||
                    !message.trim() ||
                    snapshot.activation.state !== "ACTIVE"
                  }
                >
                  <Send size={16} />
                  {busyAction ? "受付中" : "送信"}
                </button>
              </div>
              {snapshot.activation.state !== "ACTIVE" && (
                <p className="composer-gate">
                  Interfaceの理解をownerが確認するまで通常会話とExecutionは開始されません。
                </p>
              )}
            </div>
          </section>
        )}

        {view === "ledger" && (
          <section className="panel ledger">
            <div className="panel-title">
              <div>
                <p className="eyebrow">CANONICAL EVENT LEDGER</p>
                <h2>何を根拠に現在へ到達したか</h2>
              </div>
              <span>{visibleEvents.length} visible</span>
            </div>
            {visibleEvents.map((item) => (
              <article key={item.cursor}>
                <span className="cursor">{item.cursor}</span>
                <div>
                  <strong>{item.event.event_type}</strong>
                  <span>
                    {item.event.stream_id} ·{" "}
                    {new Date(item.event.occurred_at).toLocaleString("ja-JP")}
                  </span>
                </div>
                <details>
                  <summary>payload</summary>
                  <code>{JSON.stringify(item.event.payload, null, 2)}</code>
                </details>
              </article>
            ))}
          </section>
        )}

        {view === "routing" && (
          <div className="grid two-one">
            <section className="panel">
              <div className="panel-title">
                <div>
                  <p className="eyebrow">BAYESIAN ROUTING</p>
                  <h2>承認されたroutingだけを本番へ</h2>
                </div>
                <Route size={19} />
              </div>
              <div className="route-summary">
                <StatePill value={route?.state ?? "missing"} />
                <span>
                  {route?.production_objective ??
                    "公開済みRouteSnapshotはありません"}
                </span>
              </div>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Candidate</th>
                      <th>Reliability</th>
                      <th>Tokens</th>
                      <th>Cost</th>
                      <th>Q</th>
                      <th>U</th>
                      <th>R/C</th>
                    </tr>
                  </thead>
                  <tbody>
                    {scores.map((score) => (
                      <tr
                        key={score.candidate_key}
                        className={
                          selected?.candidate_key === score.candidate_key
                            ? "selected-row"
                            : ""
                        }
                      >
                        <td>{short(score.candidate_key, 26)}</td>
                        <td>{(score.reliability * 100).toFixed(1)}%</td>
                        <td>{Math.round(score.expected_tokens)}</td>
                        <td>{score.expected_cost.toFixed(3)}</td>
                        <td>{score.ranks.quality_max}</td>
                        <td>{score.ranks.expected_utility}</td>
                        <td>{score.ranks.reliability_then_cost}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
            <section className="panel">
              <div className="panel-title">
                <div>
                  <p className="eyebrow">MODEL REGISTRY</p>
                  <h2>環境まで含む候補</h2>
                </div>
                <Bot size={19} />
              </div>
              <div className="model-list">
                {snapshot.models.candidates.map((model) => (
                  <article key={model.key}>
                    <strong>{model.model_snapshot}</strong>
                    <StatePill value={model.effort} />
                    <span>
                      {model.adapter}@{model.adapter_version}
                    </span>
                    <small>{model.environment_fingerprint}</small>
                  </article>
                ))}
              </div>
            </section>
          </div>
        )}

        {view === "audit" && (
          <div className="grid three">
            <section className="panel">
              <div className="panel-title">
                <div>
                  <p className="eyebrow">EFFECT LEASES</p>
                  <h2>副作用</h2>
                </div>
                <Cable size={19} />
              </div>
              {snapshot.executions.effect_leases.map((lease) => (
                <article className="audit-row" key={lease.lease_id}>
                  <div>
                    <strong>{lease.effect_kind}</strong>
                    <span>{short(lease.lease_id)}</span>
                  </div>
                  <StatePill value={lease.state} />
                </article>
              ))}
            </section>
            <section className="panel">
              <div className="panel-title">
                <div>
                  <p className="eyebrow">BUDGET</p>
                  <h2>予約済み上限</h2>
                </div>
                <CircleDollarSign size={19} />
              </div>
              {snapshot.executions.budget_reservations.map((item) => (
                <article className="audit-row" key={item.reservation_id}>
                  <div>
                    <strong>
                      {item.amount} {item.currency}
                    </strong>
                    <span>{item.token_limit.toLocaleString()} tokens</span>
                  </div>
                </article>
              ))}
            </section>
            <section className="panel">
              <div className="panel-title">
                <div>
                  <p className="eyebrow">TOKEN LAB</p>
                  <h2>調査対象</h2>
                </div>
                <Activity size={19} />
              </div>
              {snapshot.lab.observations.map((item) => (
                <article className="audit-row" key={item.observation_id}>
                  <div>
                    <strong>
                      {item.total_input_tokens.toLocaleString()} input
                    </strong>
                    <span>{item.incident_kinds.join(", ") || "clean"}</span>
                  </div>
                </article>
              ))}
            </section>
          </div>
        )}
      </main>
    </div>
  );
}
