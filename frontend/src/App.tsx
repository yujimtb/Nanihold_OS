import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  Bot,
  Boxes,
  Cable,
  CheckCircle2,
  CircleDollarSign,
  GitMerge,
  MessageSquareText,
  RefreshCw,
  Route,
  Search,
  Send,
  ShieldCheck,
  Sparkles,
  Waypoints,
} from "lucide-react";
import { ApiClient } from "./api";
import type {
  Conversation,
  DataSpace,
  EventItem,
  Execution,
  Message,
  ModelCandidate,
  Node,
  WorkEdge,
  WorkItem,
} from "./types";

type View = "command" | "conversation" | "ledger" | "routing" | "audit";

type PilotUsage = {
  input_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
  output_tokens: number;
  cost_usd: number;
};

type Snapshot = {
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
    messages: Record<string, Message[]>;
    commitments: Array<{ commitment_id: string; statement: string; state: string }>;
    decisions: Array<{ decision_id: string; statement: string }>;
    node_memories: unknown[];
  };
  hosts: Array<{ identity: { pilot_host_id: string }; state: string }>;
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

const empty: Snapshot = {
  spaces: [],
  nodes: { items: [], capability_grants: [], reference_grants: [] },
  work: { items: [], edges: [] },
  executions: { items: [], effect_leases: [], budget_reservations: [] },
  events: { events: [], next_cursor: 0 },
  conversations: {
    items: [],
    messages: {},
    commitments: [],
    decisions: [],
    node_memories: [],
  },
  hosts: [],
  models: { candidates: [], verified_outcomes: [], evidence_cursor: 0 },
  routes: { items: [], scores: {} },
  lab: { observations: [] },
};

function short(value: string, keep = 12) {
  return value.length > keep ? `${value.slice(0, keep)}…` : value;
}

function StatePill({ value }: { value: string }) {
  return <span className={`pill state-${value}`}>{value.replaceAll("_", " ")}</span>;
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

export default function App() {
  const [token, setToken] = useState(() => sessionStorage.getItem("nanihold-token") ?? "");
  const [draftToken, setDraftToken] = useState(token);
  const [view, setView] = useState<View>("command");
  const [snapshot, setSnapshot] = useState<Snapshot>(empty);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState("");
  const [selectedConversation, setSelectedConversation] = useState("");
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);

  const client = useMemo(() => (token ? new ApiClient(token) : null), [token]);

  const refresh = useCallback(async () => {
    if (!client) return;
    setLoading(true);
    try {
      const [
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
      const next = {
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
      };
      setSnapshot(next);
      if (!selectedConversation && conversations.items[0]) {
        setSelectedConversation(conversations.items[0].conversation_id);
      }
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [client, selectedConversation]);

  useEffect(() => {
    void refresh();
    if (!client) return;
    const timer = window.setInterval(() => void refresh(), 15_000);
    return () => window.clearInterval(timer);
  }, [client, refresh]);

  function connect() {
    sessionStorage.setItem("nanihold-token", draftToken);
    setToken(draftToken);
  }

  async function sendMessage() {
    if (!client || !selectedConversation || !message.trim()) return;
    setSending(true);
    try {
      await client.post(`/api/conversations/${selectedConversation}/messages`, {
        text: message,
        idempotency_key: `web:${crypto.randomUUID()}`,
        force_new_pilot: false,
      });
      setMessage("");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSending(false);
    }
  }

  async function intervene(workItemId: string) {
    if (!client) return;
    const reason = window.prompt("停止理由を入力してください");
    if (!reason?.trim()) return;
    try {
      await client.post(`/api/work-items/${workItemId}/interventions`, {
        actor_id: snapshot.spaces[0]?.owner_id,
        reason,
        idempotency_key: `web:${crypto.randomUUID()}`,
      });
      await refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }

  if (!token) {
    return (
      <main className="login-shell">
        <section className="login-card">
          <div className="brand-mark"><Waypoints size={24} /></div>
          <p className="eyebrow">NANIHOLD CONTROL PLANE</p>
          <h1>再開できる仕事には、<br />消えない主体がいる。</h1>
          <p className="muted">
            DataSpaceのBearer tokenを入力してください。tokenはこのタブのsessionStorageにのみ保持されます。
          </p>
          <label>
            Bearer token
            <input
              type="password"
              value={draftToken}
              onChange={(event) => setDraftToken(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && connect()}
            />
          </label>
          <button className="primary" onClick={connect} disabled={!draftToken.trim()}>
            Interface Nodeへ接続
          </button>
        </section>
      </main>
    );
  }

  const activeWork = snapshot.work.items.filter((item) =>
    ["ready", "active", "paused", "blocked"].includes(item.state),
  );
  const visibleEvents = snapshot.events.events
    .filter((item) =>
      `${item.event.event_type} ${item.event.stream_id}`.toLowerCase().includes(filter.toLowerCase()),
    )
    .slice()
    .reverse();
  const messages = snapshot.conversations.messages[selectedConversation] ?? [];
  const route = snapshot.routes.items.find((item) => item.state === "published");
  const scores = route ? snapshot.routes.scores[route.snapshot_id] ?? [] : [];
  const selected = scores.find((item) => item.ranks[route?.production_objective ?? "quality_max"] === 1);
  const interfaceModel = snapshot.models.candidates.find(
    (model) => model.key === selected?.candidate_key,
  ) ?? snapshot.models.candidates[0];
  const latestPilotUsage = pilotUsage(
    snapshot.events.events
      .slice()
      .reverse()
      .find(
        (item) =>
          item.event.event_type === "interface_response_recorded"
          && item.event.stream_id === selectedConversation,
      )?.event.payload.pilot_usage,
  );

  return (
    <div className="app-shell">
      <aside>
        <div className="brand"><div className="brand-mark"><Waypoints size={20} /></div><div><strong>Nanihold</strong><span>persistent interface</span></div></div>
        <nav>
          {([
            ["command", Boxes, "Command"],
            ["conversation", MessageSquareText, "Conversation"],
            ["ledger", Activity, "Event ledger"],
            ["routing", Route, "Routing"],
            ["audit", ShieldCheck, "Audit"],
          ] as const).map(([key, Icon, label]) => (
            <button key={key} className={view === key ? "active" : ""} onClick={() => setView(key)}>
              <Icon size={17} />{label}
            </button>
          ))}
        </nav>
        <div className="connection">
          <span className={error ? "dot danger" : "dot"} />
          <div><strong>{error ? "Attention" : "Ledger connected"}</strong><span>{snapshot.spaces[0]?.data_space_id ?? "loading"}</span></div>
        </div>
      </aside>

      <main>
        <header>
          <div><p className="eyebrow">OWNER INTERFACE NODE</p><h1>{view === "command" ? "Operational picture" : view.replaceAll("_", " ")}</h1></div>
          <div className="header-actions">
            <label className="search"><Search size={15} /><input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Filter ledger" /></label>
            <button className="icon-button" onClick={() => void refresh()} aria-label="Refresh"><RefreshCw size={17} className={loading ? "spin" : ""} /></button>
          </div>
        </header>

        {error && <div className="error-banner">{error}</div>}

        {view === "command" && (
          <>
            <section className="metrics">
              <article><span>Active work</span><strong>{activeWork.length}</strong><small>{snapshot.work.items.length} total WorkItems</small></article>
              <article><span>Live executions</span><strong>{snapshot.executions.items.filter((item) => item.state === "active").length}</strong><small>{snapshot.hosts.filter((item) => item.state === "connected").length} PilotHosts connected</small></article>
              <article><span>Open commitments</span><strong>{snapshot.conversations.commitments.filter((item) => item.state === "open").length}</strong><small>owned by Interface Node</small></article>
              <article><span>Ledger cursor</span><strong>{snapshot.events.next_cursor}</strong><small>model-free projection</small></article>
            </section>
            <div className="grid two-one">
              <section className="panel">
                <div className="panel-title"><div><p className="eyebrow">WORK GRAPH</p><h2>Delegation and integration</h2></div><GitMerge size={19} /></div>
                <div className="work-list">
                  {activeWork.map((work) => (
                    <article key={work.work_item_id}>
                      <div className="work-head"><div><strong>{work.title}</strong><span>{short(work.work_item_id, 20)}</span></div><div className="work-actions"><StatePill value={work.state} />{["ready", "active", "blocked"].includes(work.state) && <button onClick={() => void intervene(work.work_item_id)}>停止</button>}</div></div>
                      <div className="work-route"><span>{short(work.delegated_to_node_id)}</span><span>executes</span><span>{short(work.integration_owner_node_id)}</span><span>integrates</span></div>
                      <div className="criteria">{work.acceptance_criteria.map((criterion) => <span key={criterion}><CheckCircle2 size={13} />{criterion}</span>)}</div>
                    </article>
                  ))}
                  {!activeWork.length && <p className="empty">No active WorkItems.</p>}
                </div>
              </section>
              <section className="panel">
                <div className="panel-title"><div><p className="eyebrow">NODE TREE</p><h2>Resident u-VSM</h2></div><Waypoints size={19} /></div>
                <div className="node-list">
                  {snapshot.nodes.items.map((node) => (
                    <article key={node.node_id}>
                      <div className="node-icon">{node.kind === "interface" ? <Sparkles size={17} /> : <Boxes size={17} />}</div>
                      <div><strong>{node.name}</strong><span>{node.kind} · {node.status}</span><div className="functions">{node.resident_functions.map((fn) => <i key={fn}>{fn}</i>)}</div></div>
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
              <p className="eyebrow">CONTINUOUS THREADS</p>
              {snapshot.conversations.items.map((item) => (
                <button key={item.conversation_id} className={selectedConversation === item.conversation_id ? "selected" : ""} onClick={() => setSelectedConversation(item.conversation_id)}>
                  <MessageSquareText size={16} /><div><strong>{short(item.conversation_id, 22)}</strong><span>{item.provider_session_id ? "provider resumed" : "memory resume"}</span></div>
                </button>
              ))}
            </div>
            <div className="chat panel">
              <div className="chat-head"><div><p className="eyebrow">INTERFACE PILOT</p><h2>{interfaceModel ? `${interfaceModel.model_snapshot} · ${interfaceModel.effort}` : "unavailable"}</h2></div><span className="model-note">{latestPilotUsage ? `${latestPilotUsage.input_tokens.toLocaleString()} input + ${latestPilotUsage.cache_creation_input_tokens.toLocaleString()} cache write + ${latestPilotUsage.cache_read_input_tokens.toLocaleString()} cache read + ${latestPilotUsage.output_tokens.toLocaleString()} output · $${latestPilotUsage.cost_usd.toFixed(4)}` : "1 call / owner turn"}</span></div>
              <div className="messages">
                {messages.map((item) => <article key={item.message_id} className={item.role}><span>{item.role}</span><p>{item.display_text}</p></article>)}
              </div>
              <div className="composer">
                <textarea value={message} onChange={(event) => setMessage(event.target.value)} placeholder="短い訂正だけでも作戦を継続できます…" />
                <button className="primary" onClick={() => void sendMessage()} disabled={sending || !message.trim()}><Send size={16} />{sending ? "送信中" : "送信"}</button>
              </div>
            </div>
          </section>
        )}

        {view === "ledger" && (
          <section className="panel ledger">
            <div className="panel-title"><div><p className="eyebrow">CANONICAL EVENT LEDGER</p><h2>Temporal drill-down</h2></div><span>{visibleEvents.length} visible</span></div>
            {visibleEvents.map((item) => (
              <article key={item.cursor}>
                <span className="cursor">{item.cursor}</span>
                <div><strong>{item.event.event_type}</strong><span>{item.event.stream_id} · {new Date(item.event.occurred_at).toLocaleString("ja-JP")}</span></div>
                <code>{JSON.stringify(item.event.payload)}</code>
              </article>
            ))}
          </section>
        )}

        {view === "routing" && (
          <div className="grid two-one">
            <section className="panel">
              <div className="panel-title"><div><p className="eyebrow">BAYESIAN ROUTING</p><h2>Three objectives, one approved snapshot</h2></div><Route size={19} /></div>
              <div className="route-summary"><StatePill value={route?.state ?? "missing"} /><span>{route?.production_objective ?? "No published RouteSnapshot"}</span></div>
              <table><thead><tr><th>Candidate</th><th>Reliability</th><th>Tokens</th><th>Cost</th><th>Q</th><th>U</th><th>R/C</th></tr></thead>
                <tbody>{scores.map((score) => <tr key={score.candidate_key} className={selected?.candidate_key === score.candidate_key ? "selected-row" : ""}><td>{short(score.candidate_key, 26)}</td><td>{(score.reliability * 100).toFixed(1)}%</td><td>{Math.round(score.expected_tokens)}</td><td>{score.expected_cost.toFixed(3)}</td><td>{score.ranks.quality_max}</td><td>{score.ranks.expected_utility}</td><td>{score.ranks.reliability_then_cost}</td></tr>)}</tbody>
              </table>
            </section>
            <section className="panel">
              <div className="panel-title"><div><p className="eyebrow">MODEL REGISTRY</p><h2>Exact environments</h2></div><Bot size={19} /></div>
              <div className="model-list">{snapshot.models.candidates.map((model) => <article key={model.key}><strong>{model.model_snapshot}</strong><StatePill value={model.effort} /><span>{model.adapter}@{model.adapter_version}</span><small>{model.environment_fingerprint}</small></article>)}</div>
            </section>
          </div>
        )}

        {view === "audit" && (
          <div className="grid three">
            <section className="panel"><div className="panel-title"><div><p className="eyebrow">EFFECT LEASES</p><h2>Side effects</h2></div><Cable size={19} /></div>{snapshot.executions.effect_leases.map((lease) => <article className="audit-row" key={lease.lease_id}><div><strong>{lease.effect_kind}</strong><span>{short(lease.lease_id)}</span></div><StatePill value={lease.state} /></article>)}</section>
            <section className="panel"><div className="panel-title"><div><p className="eyebrow">BUDGET</p><h2>Reservations</h2></div><CircleDollarSign size={19} /></div>{snapshot.executions.budget_reservations.map((item) => <article className="audit-row" key={item.reservation_id}><div><strong>{item.amount} {item.currency}</strong><span>{item.token_limit.toLocaleString()} tokens</span></div></article>)}</section>
            <section className="panel"><div className="panel-title"><div><p className="eyebrow">TOKEN LAB</p><h2>Incidents</h2></div><Activity size={19} /></div>{snapshot.lab.observations.map((item) => <article className="audit-row" key={item.observation_id}><div><strong>{item.total_input_tokens.toLocaleString()} input</strong><span>{item.incident_kinds.join(", ") || "clean"}</span></div></article>)}</section>
          </div>
        )}
      </main>
    </div>
  );
}
