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
  LoaderCircle,
  MessageSquareText,
  Network,
  RefreshCw,
  Route,
  Search,
  Send,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
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

type View = "dashboard" | "conversation" | "ledger" | "routing" | "audit";
type AuthState = "checking" | "authenticated" | "unauthenticated";

const NAV: Array<[View, typeof Boxes, string]> = [
  ["dashboard", Boxes, "ダッシュボード"],
  ["conversation", MessageSquareText, "対話"],
  ["ledger", Activity, "根拠"],
  ["routing", Route, "Routing"],
  ["audit", ShieldCheck, "監査"],
];

const VIEW_COPY: Record<View, { eyebrow: string; title: string; lede: string }> = {
  dashboard: {
    eyebrow: "LIVE INTERFACE WORKSPACE",
    title: "組織が、いま何をしているか。",
    lede: "Interface Pilotの起動状況と、進行中のWorkItem・参加ノードを一目で見渡します。",
  },
  conversation: {
    eyebrow: "CANONICAL CONVERSATION",
    title: "Interfaceとの対話",
    lede: "唯一の正本Conversationを通じて、確認済みの状況の上で作戦を続けます。",
  },
  ledger: {
    eyebrow: "CANONICAL EVENT LEDGER",
    title: "何を根拠に現在へ到達したか",
    lede: "すべての判断はEventとして残ります。ここから根拠を辿れます。",
  },
  routing: {
    eyebrow: "BAYESIAN ROUTING",
    title: "承認されたroutingだけを本番へ",
    lede: "候補モデルの信頼度・コストを比較し、公開済みRouteSnapshotを確認します。",
  },
  audit: {
    eyebrow: "OPERATIONAL AUDIT",
    title: "副作用と予算の監査",
    lede: "Effectリース、予算予約、TokenLabの調査対象を横断して確認します。",
  },
};

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
    effect_leases: Array<{ lease_id: string; effect_kind: string; state: string }>;
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
    items: Array<{ snapshot_id: string; production_objective: string; state: string }>;
    scores: Record<
      string,
      Array<{
        candidate_key: string;
        reliability: number;
        expected_tokens: number;
        expected_cost: number;
        ranks: Record<string, number>;
      }> | null
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

const ACTIVATION_COPY: Record<ActivationState, { title: string; detail: string }> = {
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

  // Once ACTIVE, collapse the panel to a compact status strip so the dashboard
  // reads as the operational cockpit rather than an onboarding wizard.
  if (activation.state === "ACTIVE") {
    return (
      <div className="activation-strip">
        <Sparkles size={17} />
        <div>
          <strong>{copy.title}</strong>
        </div>
        {activation.import_receipt && (
          <div className="facts">
            <span>
              {activation.import_receipt.sources
                .reduce((total, source) => total + source.record_count, 0)
                .toLocaleString()}{" "}
              records
            </span>
            <span>{activation.reorientation_pilot_calls} reorientation calls</span>
            <span>
              {activation.reorientation_input_tokens.toLocaleString()} in /{" "}
              {activation.reorientation_output_tokens.toLocaleString()} out
            </span>
          </div>
        )}
      </div>
    );
  }

  return (
    <section
      className={`activation-card activation-${activation.state.toLowerCase()}`}
      aria-labelledby="activation-title"
    >
      <div className="activation-head">
        <div className="activation-icon">
          {activation.state === "AWAITING_OWNER_CONFIRMATION" ? (
            <ShieldCheck size={22} />
          ) : (
            <History size={22} />
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
              index < currentIndex ? "complete" : index === currentIndex ? "current" : ""
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
            {activation.import_receipt.sources
              .reduce((total, source) => total + source.record_count, 0)
              .toLocaleString()}{" "}
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
        <button className="primary-button activation-action" onClick={onStart} disabled={busy}>
          <History size={16} />
          {busy ? "開始中…" : "Interface Pilotに履歴読解を開始させる"}
        </button>
      )}
      {activation.state === "REORIENTATION_ONLY" &&
        (activation.reorientation_error ? (
          <div className="reading-status error-banner" role="alert">
            <TriangleAlert size={16} />
            <span>
              履歴読解は安全に停止しました（{activation.reorientation_error}）。
              ExecutionとEffectは開始されていません。
            </span>
            <button className="primary-button" onClick={onStart} disabled={busy}>
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
            <button className="primary-button" onClick={onStart} disabled={busy}>
              <RefreshCw size={16} />
              {busy ? "再評価中…" : "再評価を開始"}
            </button>
          </div>
        ) : (
          <div className="reading-status error-banner" role="alert">
            <TriangleAlert size={16} />
            再オリエンテーション状態に開始理由がありません。運用監査が必要です。
          </div>
        ))}
      {activation.assessment && (
        <ReorientationBrief
          assessment={activation.assessment}
          commitments={snapshot.conversations.commitments}
          work={snapshot.work.items}
        />
      )}
      {activation.state === "AWAITING_OWNER_CONFIRMATION" &&
        activation.assessment &&
        (activation.assessment.resume_work_item_ids.length === 0 ? (
          <div className="reading-status error-banner" role="alert">
            <TriangleAlert size={16} />
            <span>
              実在する未完WorkItemが再開候補に含まれていません。この理解は承認できません。
            </span>
            <button className="secondary-button" onClick={onRevise} disabled={busy}>
              <RefreshCw size={16} />
              {busy ? "再評価を開始中…" : "未完WorkItemを含めて再評価"}
            </button>
          </div>
        ) : (
          <div className="approval-box">
            <label htmlFor="owner-correction">訂正があれば1行ずつ入力してください</label>
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
              <button className="primary-button" onClick={onApprove} disabled={busy}>
                <ShieldCheck size={16} />
                {busy ? "確認を保存中…" : "理解を確認してInterface Pilotを起動"}
              </button>
            </div>
          </div>
        ))}
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
  const [view, setView] = useState<View>("dashboard");
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
      setSelectedConversation(
        (existing) => existing || conversations.items[0]?.conversation_id || "",
      );
      setAuthState("authenticated");
      setError(null);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        setAuthState("unauthenticated");
        setSnapshot(null);
        setError(null);
      } else {
        // A non-auth failure means the owner session is valid but a projection
        // could not be read. Surface it instead of hanging on the boot spinner.
        setAuthState((current) => (current === "checking" ? "authenticated" : current));
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

  // ---- gates before the authenticated shell -------------------------------
  if (!snapshot) {
    if (authState === "unauthenticated") {
      return (
        <main className="login-shell">
          <section className="login-card" aria-labelledby="login-title">
            <div className="login-mark">
              <KeyRound size={23} />
            </div>
            <p className="eyebrow">OWNER DEVICE</p>
            <h1 id="login-title">この端末をInterface Pilotにつなぎます</h1>
            <p className="muted">
              Naniholdが発行した短時間有効なowner bootstrap linkを開くか、codeを入力してください。認証情報はHttpOnly
              cookieとして保存されます。
            </p>
            <label htmlFor="bootstrap-code">Owner bootstrap code</label>
            <input
              id="bootstrap-code"
              type="password"
              autoComplete="one-time-code"
              value={bootstrapCode}
              onChange={(event) => setBootstrapCode(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && void exchangeBootstrap()}
            />
            <p className="device-id">
              この端末: <code>{deviceId}</code>
            </p>
            {error && (
              <div className="error-banner" role="alert">
                <TriangleAlert size={16} />
                {error}
              </div>
            )}
            <button
              className="primary-button"
              onClick={() => void exchangeBootstrap()}
              disabled={busyAction || !bootstrapCode.trim()}
            >
              <KeyRound size={16} />
              {busyAction ? "認証中…" : "この端末を認証"}
            </button>
          </section>
        </main>
      );
    }
    if (error) {
      return (
        <main className="error-page">
          <div className="login-mark">
            <TriangleAlert size={23} />
          </div>
          <p className="eyebrow">DASHBOARD UNAVAILABLE</p>
          <h1>状態を読み込めませんでした</h1>
          <code>{error}</code>
          <div className="actions">
            <button className="primary-button" onClick={() => void refresh()} disabled={loading}>
              <RefreshCw size={16} className={loading ? "spin" : ""} />
              再試行
            </button>
            <button
              className="ghost-button"
              onClick={() => {
                setSnapshot(null);
                setError(null);
                setAuthState("unauthenticated");
              }}
            >
              <KeyRound size={16} />
              認証し直す
            </button>
          </div>
        </main>
      );
    }
    return (
      <div className="boot">
        <LoaderCircle className="spin" size={22} />
        Nanihold OSを起動しています
      </div>
    );
  }

  // ---- derived data -------------------------------------------------------
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
    (item) => item.ranks[route?.production_objective ?? "quality_max"] === 1,
  );
  const interfaceModel =
    snapshot.models.candidates.find((model) => model.key === selected?.candidate_key) ??
    snapshot.models.candidates[0];
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

  const copy = VIEW_COPY[view];

  return (
    <div className="shell">
      <header className="topbar">
        <button className="brand" onClick={() => setView("dashboard")}>
          <span className="brand-mark">N</span>
          <span>Nanihold OS</span>
        </button>
        <nav className="topbar-nav" aria-label="メインナビゲーション">
          {NAV.map(([key, Icon, label]) => (
            <button
              key={key}
              className={`nav-tab ${view === key ? "active" : ""}`}
              onClick={() => setView(key)}
              aria-current={view === key ? "page" : undefined}
            >
              <Icon size={15} />
              {label}
            </button>
          ))}
        </nav>
        <div className="conn-badge" title={snapshot.spaces[0]?.data_space_id}>
          <span className={error ? "dot danger" : "dot"} />
          <strong>{error ? "確認が必要" : "接続中"}</strong>
          <span className="conn-space">{snapshot.spaces[0]?.data_space_id}</span>
        </div>
        <button
          className="icon-button"
          onClick={() => void refresh()}
          aria-label="最新状態へ更新"
        >
          <RefreshCw size={16} className={loading ? "spin" : ""} />
        </button>
      </header>

      <main className="page">
        <section className="home-intro">
          <div>
            <p className="eyebrow">{copy.eyebrow}</p>
            <h1>{copy.title}</h1>
          </div>
        </section>
        <p className="lede">{copy.lede}</p>

        {error && (
          <div className="error-banner" role="alert" style={{ marginTop: 20 }}>
            <TriangleAlert size={16} />
            {error}
            <button onClick={() => setError(null)} aria-label="エラーを閉じる">
              ×
            </button>
          </div>
        )}

        {view === "dashboard" && (
          <div className="dashboard-page">
            <ActivationPanel
              snapshot={snapshot}
              busy={busyAction}
              correction={correction}
              onCorrection={setCorrection}
              onStart={() => void startReorientation()}
              onApprove={() => void approveReorientation()}
              onRevise={() => void reviseReorientation()}
            />

            <section className="metric-grid">
              <article className="metric">
                <span>現在</span>
                <strong>{activeWork.length - waitingWork.length}</strong>
                <small>
                  {snapshot.executions.items.filter((item) => item.state === "active").length}{" "}
                  executions active
                </small>
              </article>
              <article className="metric">
                <span>待っています</span>
                <strong>{waitingWork.length}</strong>
                <small>
                  {snapshot.activation.state === "AWAITING_OWNER_CONFIRMATION"
                    ? "owner confirmationを含む"
                    : "paused / blocked"}
                </small>
              </article>
              <article className="metric">
                <span>委任</span>
                <strong>
                  {new Set(activeWork.map((item) => item.delegated_to_node_id)).size}
                </strong>
                <small>
                  {snapshot.hosts.filter((item) => item.state === "connected").length}{" "}
                  PilotHosts connected
                </small>
              </article>
              <article className="metric">
                <span>費用・quota</span>
                <strong>
                  {latestPilotUsage ? `$${latestPilotUsage.cost_usd.toFixed(3)}` : "—"}
                </strong>
                <small>
                  {quotaHost?.quota_remaining_percent !== undefined
                    ? `${quotaHost.quota_remaining_percent}% remaining`
                    : "quota telemetry待ち"}
                </small>
              </article>
              <article className="metric">
                <span>根拠</span>
                <strong>{evidenceCount}</strong>
                <small>citations + completion evidence</small>
              </article>
            </section>

            <div className="dashboard-grid">
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
                    <article key={work.work_item_id} className={`work-card state-${work.state}`}>
                      <div className="work-head">
                        <div>
                          <strong>{work.title}</strong>
                          <span className="work-id">{short(work.work_item_id, 24)}</span>
                        </div>
                        <div className="work-actions">
                          <StatePill value={work.state} />
                          {["ready", "active", "blocked"].includes(work.state) && (
                            <button
                              className="stop-button"
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
                  <Network size={19} />
                </div>
                <div className="node-list">
                  {snapshot.nodes.items.map((node) => (
                    <article key={node.node_id} className="node-row">
                      <div className="node-icon">
                        {node.kind === "interface" ? (
                          <Sparkles size={17} />
                        ) : (
                          <Boxes size={17} />
                        )}
                      </div>
                      <div>
                        <strong>{node.name}</strong>
                        <span className="node-kind">
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
                  {!snapshot.nodes.items.length && (
                    <p className="empty">ノードはまだ登録されていません。</p>
                  )}
                </div>
              </section>
            </div>
          </div>
        )}

        {view === "conversation" && (
          <div className="conversation-page">
            <div className="conversation-layout">
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
                      className={selectedConversation === item.conversation_id ? "selected" : ""}
                      onClick={() => setSelectedConversation(item.conversation_id)}
                    >
                      <MessageSquareText size={16} />
                      <div>
                        <strong>{item.title}</strong>
                        <span>
                          {surfaces.map((surface) => surface.surface).join(" · ") || "Web"}
                          {" · "}
                          {sessions.length} Pilot sessions
                        </span>
                      </div>
                    </button>
                  );
                })}
                {!snapshot.conversations.items.length && (
                  <p className="empty">Conversationはまだありません。</p>
                )}
              </div>
              <div className="chat-panel panel">
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
                    className="primary-button"
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
            </div>
          </div>
        )}

        {view === "ledger" && (
          <div className="ledger-page">
            <div className="content-heading">
              <div>
                <p className="eyebrow">EVENT LEDGER</p>
                <h2>{visibleEvents.length} 件を表示</h2>
              </div>
              <label className="conn-badge" style={{ borderRadius: 10 }}>
                <Search size={14} />
                <span className="sr-only">Eventを絞り込む</span>
                <input
                  value={filter}
                  onChange={(event) => setFilter(event.target.value)}
                  placeholder="根拠を検索"
                  style={{ border: 0, outline: 0, background: "transparent", color: "inherit" }}
                />
              </label>
            </div>
            <div className="event-list">
              {visibleEvents.map((item) => (
                <article className="event-row" key={item.cursor}>
                  <span className="cursor">#{item.cursor}</span>
                  <div>
                    <strong>{item.event.event_type}</strong>
                    <span className="event-meta">
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
              {!visibleEvents.length && (
                <p className="empty" style={{ paddingTop: 18 }}>
                  該当するEventはありません。
                </p>
              )}
            </div>
          </div>
        )}

        {view === "routing" && (
          <div className="routing-page">
            <div className="two-col">
              <section className="panel">
                <div className="panel-title">
                  <div>
                    <p className="eyebrow">ROUTE SNAPSHOT</p>
                    <h2>候補の比較</h2>
                  </div>
                  <Route size={19} />
                </div>
                <div className="route-summary">
                  <StatePill value={route?.state ?? "missing"} />
                  <span>
                    {route?.production_objective ?? "公開済みRouteSnapshotはありません"}
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
                            selected?.candidate_key === score.candidate_key ? "selected-row" : ""
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
                      {!scores.length && (
                        <tr>
                          <td colSpan={7} style={{ color: "var(--muted)" }}>
                            スコア可能な候補がありません。
                          </td>
                        </tr>
                      )}
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
                  {!snapshot.models.candidates.length && (
                    <p className="empty">登録済みモデル候補はありません。</p>
                  )}
                </div>
              </section>
            </div>
          </div>
        )}

        {view === "audit" && (
          <div className="audit-page">
            <div className="audit-grid">
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
                {!snapshot.executions.effect_leases.length && (
                  <p className="empty">保留中の副作用リースはありません。</p>
                )}
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
                {!snapshot.executions.budget_reservations.length && (
                  <p className="empty">予算予約はありません。</p>
                )}
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
                      <strong>{item.total_input_tokens.toLocaleString()} input</strong>
                      <span>{item.incident_kinds.join(", ") || "clean"}</span>
                    </div>
                  </article>
                ))}
                {!snapshot.lab.observations.length && (
                  <p className="empty">TokenLabの観測はまだありません。</p>
                )}
              </section>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
