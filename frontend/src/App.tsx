import {
  ArrowLeft,
  ArrowUpRight,
  Bot,
  CircleStop,
  CornerDownLeft,
  FileText,
  History,
  LoaderCircle,
  MessageCircle,
  Network,
  OctagonAlert,
  Pause,
  Play,
  Plus,
  RotateCcw,
  Send,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent as ReactWheelEvent } from "react";
import ReactMarkdown from "react-markdown";
import { api } from "./api";
import {
  isRecentlyActive,
  layoutTopology,
  nodeStatusClass,
  nodeStatusColor,
  NODE_HEIGHT,
  NODE_WIDTH,
  STAGE_PADDING,
} from "./topologyLayout";
import type {
  AppConfig,
  ChatMessage,
  ChatSession,
  RunDetail,
  RunStatus,
  RunSummary,
  TimelineItem,
  Topology,
  NodeStatus,
} from "./types";

const STATUS_LABELS: Record<RunStatus, string> = {
  queued: "準備中",
  running: "実行中",
  interrupting: "指示を反映中",
  waiting_for_user: "判断待ち",
  completed: "完了",
  cancelled: "停止済み",
  failed: "失敗",
};

function formatDate(value: string) {
  return new Intl.DateTimeFormat("ja-JP", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function App() {
  const [view, setView] = useState<"home" | "chat" | "run">("home");
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selected, setSelected] = useState<RunDetail | null>(null);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refreshRuns = useCallback(async () => {
    const data = await api.listRuns();
    setRuns(data);
  }, []);

  useEffect(() => {
    Promise.all([refreshRuns(), api.config().then(setConfig)])
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, [refreshRuns]);

  const openRun = useCallback(async (runId: string) => {
    setError("");
    try {
      setSelected(await api.getRun(runId));
      setView("run");
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    if (!selected || !["queued", "running", "interrupting", "waiting_for_user"].includes(selected.status)) return;
    const source = new EventSource(api.streamUrl(selected.run_id));
    source.addEventListener("run", (event) => {
      const detail = JSON.parse((event as MessageEvent).data) as RunDetail;
      setSelected(detail);
      setRuns((current) => {
        const summary: RunSummary = detail;
        const rest = current.filter((item) => item.run_id !== detail.run_id);
        return [summary, ...rest];
      });
      if (detail.status === "completed" && "Notification" in window && Notification.permission === "granted") {
        new Notification("Nanihold OS", { body: "タスクが完了しました。" });
      }
    });
    source.onerror = () => source.close();
    return () => source.close();
  }, [selected?.run_id, selected?.status]);

  const handleCreated = (detail: RunDetail) => {
    setSelected(detail);
    setView("run");
    setRuns((current) => [detail, ...current.filter((run) => run.run_id !== detail.run_id)]);
  };

  const handleDelete = async (runId: string) => {
    if (!window.confirm("このRunのログ、添付、結果をすべて削除しますか？")) return;
    await api.delete(runId);
    setSelected(null);
    setView("home");
    await refreshRuns();
  };

  if (loading) {
    return <div className="boot"><LoaderCircle className="spin" /> Nanihold OSを起動しています</div>;
  }

  return (
    <div className="shell">
      <header className="topbar">
        <button className="brand" onClick={() => { setSelected(null); setView("home"); }}>
          <span className="brand-mark">N</span>
          <span>Nanihold OS</span>
        </button>
        <nav className="topbar-nav" aria-label="メインナビゲーション">
          <button className={`nav-tab ${view === "home" ? "active" : ""}`} onClick={() => { setSelected(null); setView("home"); }}>
            ダッシュボード
          </button>
          <button className={`nav-tab ${view === "chat" ? "active" : ""}`} onClick={() => setView("chat")}>
            <MessageCircle size={15} /> 対話
          </button>
        </nav>
        <div className="model-badge">
          <span className={`model-dot ${config?.demo_mode ? "demo" : ""}`} />
          {config?.model}
          {config?.demo_mode && <span className="muted">demo</span>}
        </div>
      </header>

      {error && (
        <div className="global-error">
          <span>{error}</span>
          <button onClick={() => setError("")}><X size={16} /></button>
        </div>
      )}

      {view === "chat" ? (
        <ChatView runs={runs} onRunCreated={handleCreated} onOpenRun={openRun} />
      ) : selected && view === "run" ? (
        <RunView
          run={selected}
          onBack={() => {
            setSelected(null);
            refreshRuns();
          }}
          onChange={setSelected}
          onDelete={() => handleDelete(selected.run_id)}
        />
      ) : (
        <Home runs={runs} config={config} onCreated={handleCreated} onOpen={openRun} onError={setError} />
      )}
    </div>
  );
}

function Home({
  runs,
  config,
  onCreated,
  onOpen,
  onError,
}: {
  runs: RunSummary[];
  config: AppConfig | null;
  onCreated: (run: RunDetail) => void;
  onOpen: (runId: string) => void;
  onError: (message: string) => void;
}) {
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const activeRun = runs.find((run) => ["queued", "running", "interrupting", "waiting_for_user"].includes(run.status));

  const submit = async () => {
    if (!description.trim() || submitting) return;
    setSubmitting(true);
    onError("");
    try {
      onCreated(await api.createRun(description));
      if ("Notification" in window && Notification.permission === "default") {
        Notification.requestPermission();
      }
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main>
      <section className="hero">
        <div className="hero-copy">
          <p className="eyebrow">LOCAL VSM WORKSPACE</p>
          <h1>考える仕事を、<br />見えるかたちで進める。</h1>
          <p className="lede">
            依頼を入力すると、環境分析から方針決定、実行、監査までを
            複数の役割が引き継ぎます。進行中も、あなたの判断を差し込めます。
          </p>
        </div>

        <div className="composer">
          {config?.demo_mode && (
            <div className="demo-note">
              <Sparkles size={16} />
              明示された fake backend で起動中です。実 backend は <code>vsm.toml</code> の <code>[agents]</code> で設定できます。
            </div>
          )}
          <textarea
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="何を進めますか？ 日本語で具体的に書いてください。"
            rows={7}
            disabled={Boolean(activeRun)}
          />
          <div className="composer-footer">
            <span className="composer-hint">ローカル API から安全に投入されます</span>
            <button
              className="primary-button"
              onClick={submit}
              disabled={!description.trim() || submitting || Boolean(activeRun)}
            >
              {submitting ? <LoaderCircle className="spin" size={18} /> : <ArrowUpRight size={18} />}
              {activeRun ? "実行中のタスクがあります" : "実行する"}
            </button>
          </div>
        </div>
      </section>

      <section className="history-section">
        <div className="section-heading">
          <div>
            <p className="eyebrow">RUN ARCHIVE</p>
            <h2>最近の実行</h2>
          </div>
          <span>{runs.length} runs</span>
        </div>
        {runs.length === 0 ? (
          <div className="empty-state">
            <History size={24} />
            <p>まだ実行履歴はありません。</p>
          </div>
        ) : (
          <div className="run-grid">
            {runs.map((run) => (
              <button className="run-card" key={run.run_id} onClick={() => onOpen(run.run_id)}>
                <div className="run-card-top">
                  <Status status={run.status} />
                  <span>{formatDate(run.updated_at)}</span>
                </div>
                <h3>{run.title}</h3>
                <div className="run-card-bottom">
                  <span>{run.current_stage}</span>
                  <span>G{run.generation}</span>
                </div>
                {["queued", "running", "interrupting"].includes(run.status) && (
                  <div className="mini-progress"><span style={{ width: `${run.progress}%` }} /></div>
                )}
              </button>
            ))}
          </div>
        )}
      </section>
    </main>
  );
}

function RunView({
  run,
  onBack,
  onChange,
  onDelete,
}: {
  run: RunDetail;
  onBack: () => void;
  onChange: (run: RunDetail) => void;
  onDelete: () => void;
}) {
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  const active = ["queued", "running", "interrupting"].includes(run.status);

  const action = async (operation: () => Promise<RunDetail>) => {
    setBusy(true);
    try {
      onChange(await operation());
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="run-page">
      <button className="back-button" onClick={onBack}><ArrowLeft size={17} /> 履歴へ戻る</button>
      <section className="run-header">
        <div className="run-title">
          <Status status={run.status} />
          <h1>{run.title}</h1>
          <p>{run.description}</p>
        </div>
        <div className="run-meta">
          <span>{formatDate(run.created_at)}</span>
          <span>Generation {run.generation}</span>
          {run.runtimes.length > 0 && (
            <span>{run.runtimes.map((runtime) => `${runtime.backend}${runtime.model ? ` / ${runtime.model}` : ""}`).join(" · ")}</span>
          )}
        </div>
      </section>

      <section className="progress-panel">
        <div className="progress-copy">
          <span className="eyebrow">CURRENT PHASE</span>
          <strong>{run.current_stage}</strong>
        </div>
        <span className="progress-number">{run.progress}%</span>
        <div className="progress-track"><span style={{ width: `${run.progress}%` }} /></div>
      </section>

      {active && (
        <section className="intervention-panel">
          <div>
            <h2>進行中の判断に口を挟む</h2>
            <p>S5 に追加指示を届け、現在の組織と文脈を保ったまま反映します。</p>
          </div>
          <div className="instruction-row">
            <input
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              placeholder="例: 実装速度より保守性を優先して"
              onKeyDown={(event) => {
                if (event.key === "Enter" && instruction.trim()) {
                  setBusy(true);
                  api.instruct(run.run_id, instruction)
                    .then(() => setInstruction(""))
                    .finally(() => setBusy(false));
                }
              }}
            />
            <button
              className="secondary-button"
              disabled={!instruction.trim() || busy}
              onClick={() => {
                setBusy(true);
                api.instruct(run.run_id, instruction)
                  .then(() => setInstruction(""))
                  .finally(() => setBusy(false));
              }}
            >
              <Send size={17} /> 指示する
            </button>
            <button className="stop-button" disabled={busy} onClick={() => action(() => api.cancel(run.run_id))}>
              <CircleStop size={17} /> 停止
            </button>
          </div>
        </section>
      )}

      {run.status === "waiting_for_user" && (
        <section className="decision-panel">
          <div>
            <h2>自動再試行を終えました</h2>
            <p>{run.error}</p>
          </div>
          <div className="decision-actions">
            <button className="secondary-button" onClick={() => action(() => api.retry(run.run_id))}>
              <RotateCcw size={17} /> もう一度試す
            </button>
            <button className="secondary-button" onClick={() => action(() => api.usePartial(run.run_id))}>
              部分結果をまとめる
            </button>
            <button className="stop-button" onClick={() => action(() => api.cancel(run.run_id))}>中止</button>
          </div>
        </section>
      )}

      <div className="run-content run-live-layout">
        <section className="organization-column">
          <OrganizationView runId={run.run_id} active={active} />
        </section>

        <section className="timeline-column">
          <div className="content-heading">
            <p className="eyebrow">PROCESS LOG</p>
            <h2>処理過程</h2>
          </div>
          <Timeline items={run.timeline} />
        </section>
      </div>

      <section className="run-output-panel">
        <div className="content-heading">
          <p className="eyebrow">FINAL OUTPUT</p>
          <h2>最終回答</h2>
        </div>
        {run.final_answer ? (
          <article className="markdown"><ReactMarkdown>{run.final_answer}</ReactMarkdown></article>
        ) : (
          <div className="result-placeholder">
            {active ? <LoaderCircle className="spin" size={22} /> : <Plus size={22} />}
            <p>{active ? "各担当の結果を集めています。" : "最終回答はまだありません。"}</p>
          </div>
        )}
        {run.artifacts.length > 0 && (
          <div className="attachment-list">
            <span className="eyebrow">DOWNLOADS</span>
            {run.artifacts.map((artifact) => (
              <a key={artifact.name} href={api.artifactUrl(run.run_id, artifact.name)}>
                <FileText size={16} />
                <span>{artifact.name}</span>
                <small>{Math.max(1, Math.ceil(artifact.size / 1024))} KB</small>
              </a>
            ))}
          </div>
        )}
        {run.attachments.length > 0 && (
          <div className="attachment-list">
            <span className="eyebrow">ATTACHMENTS</span>
            {run.attachments.map((attachment) => (
              <a
                key={attachment.attachment_id}
                href={api.attachmentUrl(run.run_id, attachment.attachment_id)}
              >
                <FileText size={16} />
                <span>{attachment.name}</span>
                <small>{Math.ceil(attachment.size / 1024)} KB</small>
              </a>
            ))}
          </div>
        )}
      </section>

      {!active && (
        <div className="danger-zone">
          <button onClick={onDelete}><Trash2 size={16} /> このRunを削除</button>
        </div>
      )}
    </main>
  );
}

function ChatView({
  runs,
  onRunCreated,
  onOpenRun,
}: {
  runs: RunSummary[];
  onRunCreated: (detail: RunDetail) => void;
  onOpenRun: (runId: string) => void;
}) {
  const [session, setSession] = useState<ChatSession | null>(null);
  const [backend, setBackend] = useState<"claude-code" | "codex">("claude-code");
  const [model, setModel] = useState("");
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [creating, setCreating] = useState(false);
  const [bridgeBusy, setBridgeBusy] = useState("");
  const [targetRunId, setTargetRunId] = useState("");
  const [error, setError] = useState("");
  const activeRuns = runs.filter((run) => ["queued", "running", "interrupting", "waiting_for_user"].includes(run.status));

  useEffect(() => {
    if (!targetRunId || !activeRuns.some((run) => run.run_id === targetRunId)) {
      setTargetRunId(activeRuns[0]?.run_id || "");
    }
  }, [activeRuns, targetRunId]);

  const createSession = async (nextBackend = backend) => {
    setCreating(true);
    setError("");
    try {
      const next = await api.createChat(nextBackend, model);
      localStorage.setItem("nanihold.chat_id", next.chat_id);
      setSession(next);
      setBackend(next.backend);
      setModel(next.model || "");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setCreating(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    const restore = async () => {
      setLoading(true);
      const savedId = localStorage.getItem("nanihold.chat_id");
      try {
        if (!savedId) {
          const next = await api.createChat("claude-code");
          if (!cancelled) {
            localStorage.setItem("nanihold.chat_id", next.chat_id);
            setSession(next);
          }
        } else {
          const next = await api.getChat(savedId);
          if (!cancelled) {
            setSession(next);
            setBackend(next.backend);
            setModel(next.model || "");
          }
        }
      } catch (err) {
        if (!cancelled) setError((err as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    restore();
    return () => { cancelled = true; };
  }, []);

  const send = async () => {
    if (!session || !text.trim() || sending) return;
    const prompt = text.trim();
    setText("");
    setSending(true);
    setError("");
    const optimistic: ChatMessage = {
      message_id: `local-${Date.now()}`,
      role: "user",
      text: prompt,
      tokens: 0,
      tokens_in: 0,
      tokens_out: 0,
      tokens_cache_read: 0,
      latency_ms: 0,
      created_at: new Date().toISOString(),
    };
    setSession((current) => current ? { ...current, messages: [...current.messages, optimistic] } : current);
    try {
      await api.sendChatMessage(session.chat_id, prompt);
      setSession(await api.getChat(session.chat_id));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSending(false);
    }
  };

  const bridgeAsRun = async (message: ChatMessage) => {
    setBridgeBusy(message.message_id);
    setError("");
    try {
      onRunCreated(await api.createRun(message.text));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBridgeBusy("");
    }
  };

  const bridgeAsInstruction = async (message: ChatMessage) => {
    if (!targetRunId) return;
    setBridgeBusy(message.message_id);
    setError("");
    try {
      await api.instruct(targetRunId, message.text);
      onOpenRun(targetRunId);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBridgeBusy("");
    }
  };

  return (
    <main className="chat-page">
      <section className="chat-header">
        <div>
          <p className="eyebrow">SELF-HOSTING CONSOLE</p>
          <h1><MessageCircle size={27} /> Naniholdと対話する</h1>
          <p>Claude Code / Codex と会話しながら、このリポジトリを調べて開発できます。</p>
        </div>
        <div className="chat-session-controls">
          <label>Backend
            <select value={backend} onChange={(event) => setBackend(event.target.value as "claude-code" | "codex")} disabled={creating || sending}>
              <option value="claude-code">Claude Code</option>
              <option value="codex">Codex</option>
            </select>
          </label>
          <label>Model
            <input value={model} onChange={(event) => setModel(event.target.value)} placeholder="既定モデル" disabled={creating || sending} />
          </label>
          <button className="secondary-button" onClick={() => createSession()} disabled={creating || sending}>
            <Plus size={16} /> 新しい対話
          </button>
        </div>
      </section>

      {error && <div className="chat-error">{error}</div>}
      <section className="chat-panel">
        <div className="chat-panel-top">
          <div className="chat-runtime"><span className="model-dot" /> {session?.backend || backend} <span>·</span> {session?.model || "既定モデル"}</div>
          <div className="chat-total">累計 {session?.total_tokens.toLocaleString() || "0"} tokens</div>
        </div>
        <div className="chat-messages">
          {loading ? (
            <div className="chat-empty"><LoaderCircle className="spin" size={20} /> 対話セッションを復元しています</div>
          ) : session?.messages.length ? session.messages.map((message) => (
            <article className={`chat-message ${message.role}`} key={message.message_id}>
              <div className="chat-avatar">{message.role === "assistant" ? <Bot size={16} /> : <CornerDownLeft size={16} />}</div>
              <div className="chat-bubble-wrap">
                <div className="chat-bubble">
                  {message.role === "assistant" ? <ReactMarkdown>{message.text}</ReactMarkdown> : <p>{message.text}</p>}
                </div>
                <div className="chat-message-meta">
                  {message.role === "assistant" && <span>{message.tokens.toLocaleString()} tokens · {(message.latency_ms / 1000).toFixed(1)}s</span>}
                  <button disabled={Boolean(bridgeBusy) || creating} onClick={() => bridgeAsRun(message)}>このメッセージをRunとして実行</button>
                  {activeRuns.length > 0 && <button disabled={Boolean(bridgeBusy) || creating} onClick={() => bridgeAsInstruction(message)}>実行中Runへ指示として送る</button>}
                </div>
              </div>
            </article>
          )) : (
            <div className="chat-empty"><MessageCircle size={24} /> ここからNaniholdの開発を始められます。</div>
          )}
          {sending && <div className="chat-thinking"><LoaderCircle className="spin" size={17} /> Claudeが応答を作成中…</div>}
        </div>
        {activeRuns.length > 0 && (
          <div className="chat-bridge-bar">
            <span>指示を届けるRun</span>
            <select value={targetRunId} onChange={(event) => setTargetRunId(event.target.value)}>
              {activeRuns.map((run) => <option key={run.run_id} value={run.run_id}>{run.title}</option>)}
            </select>
          </div>
        )}
        <div className="chat-composer">
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                send();
              }
            }}
            placeholder="Naniholdに依頼する内容を入力…（Enterで送信、Shift+Enterで改行）"
            rows={4}
            disabled={loading || sending || !session}
          />
          <button className="primary-button" onClick={send} disabled={loading || sending || !session || !text.trim()}>
            {sending ? <LoaderCircle className="spin" size={17} /> : <Send size={17} />} 送信
          </button>
        </div>
      </section>
    </main>
  );
}

const ROLE_LABELS: Record<string, string> = {
  S5_POLICY: "方針・統括",
  S4_SCANNER: "環境監視",
  S3_ALLOCATOR: "実行配分",
  S3STAR_AUDITOR: "独立監査",
  S2_COORDINATOR: "調整",
  S1_WORKER: "実行担当",
};

const NODE_STATUS_LABELS: Record<NodeStatus, string> = {
  CREATED: "準備中",
  RUNNING: "実行中",
  IDLE: "待機中",
  SUSPENDED: "休眠",
  WAITING: "判断待ち",
  COMPLETED: "完了",
  TERMINATED: "停止",
  FAILED: "失敗",
};

function OrganizationView({ runId, active }: { runId: string; active: boolean }) {
  const [topology, setTopology] = useState<Topology | null>(null);
  const [selectedNode, setSelectedNode] = useState<string>("");
  const [instruction, setInstruction] = useState("");
  const [signal, setSignal] = useState("");
  const [statement, setStatement] = useState("");
  const [reviewResponse, setReviewResponse] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const dragStart = useRef<{ x: number; y: number; panX: number; panY: number } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await api.topology(runId);
      setTopology(next);
      setSelectedNode((current) => (
        current && next.nodes.some((node) => node.node_id === current)
          ? current
          : next.nodes[0]?.node_id || ""
      ));
    } catch (err) {
      setError((err as Error).message);
    }
  }, [runId]);

  useEffect(() => {
    refresh();
    if (!active) return;
    const timer = window.setInterval(refresh, 1500);
    return () => window.clearInterval(timer);
  }, [active, refresh]);

  const layout = useMemo(() => topology ? layoutTopology(topology.nodes) : null, [topology]);
  const selected = topology?.nodes.find((node) => node.node_id === selectedNode) || null;

  const updateTopology = (update: (current: Topology) => Topology) => {
    setTopology((current) => current ? update(current) : current);
  };

  const perform = async (
    operation: () => Promise<unknown>,
    update?: (result: unknown) => void,
    clear?: () => void,
  ) => {
    setBusy(true);
    setError("");
    try {
      const result = await operation();
      update?.(result);
      clear?.();
      // The event writer is asynchronous. Keep the acknowledgement visible
      // immediately, then let the normal live poll reconcile the projection.
      window.setTimeout(refresh, 350);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest(".node-card")) return;
    dragStart.current = { x: event.clientX, y: event.clientY, panX: pan.x, panY: pan.y };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragStart.current) return;
    setPan({
      x: dragStart.current.panX + event.clientX - dragStart.current.x,
      y: dragStart.current.panY + event.clientY - dragStart.current.y,
    });
  };
  const handlePointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    dragStart.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };
  const handleWheel = (event: ReactWheelEvent<HTMLDivElement>) => {
    event.preventDefault();
    setZoom((current) => Math.min(1.8, Math.max(0.55, current * (event.deltaY < 0 ? 1.1 : 0.9))));
  };

  const setNodeStatus = (status: NodeStatus, activity: string) => {
    if (!selectedNode) return;
    updateTopology((current) => ({
      ...current,
      nodes: current.nodes.map((node) => node.node_id === selectedNode
        ? { ...node, status, activity }
        : node),
    }));
  };

  return (
    <section className="organization-panel">
      <div className="organization-heading">
        <div>
          <p className="eyebrow">LIVE ORGANIZATION</p>
          <h2><Network size={20} /> 組織図</h2>
        </div>
        <span className={`live-indicator ${active ? "on" : ""}`}><i /> {active ? "ライブ" : "最終状態"}</span>
      </div>
      {error && <p className="organization-error">{error}</p>}
      {!topology || !layout ? (
        <div className="topology-loading"><LoaderCircle className="spin" /> 組織を再構成しています</div>
      ) : topology.nodes.length === 0 ? (
        <div className="topology-loading">組織ノードを待っています</div>
      ) : (
        <>
          <div className="topology-toolbar">
            <span>{topology.nodes.length} nodes · {active ? "状態を自動更新" : "保存された最終状態"}</span>
            <div className="topology-zoom-controls">
              <button aria-label="縮小" onClick={() => setZoom((current) => Math.max(0.55, current - 0.1))}>−</button>
              <strong>{Math.round(zoom * 100)}%</strong>
              <button aria-label="拡大" onClick={() => setZoom((current) => Math.min(1.8, current + 0.1))}>＋</button>
              <button aria-label="表示をリセット" onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }); }}>リセット</button>
            </div>
          </div>

          <div
            className="topology-viewport"
            onWheel={handleWheel}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={handlePointerUp}
            onPointerCancel={handlePointerUp}
          >
            <div
              className="topology-stage"
              style={{
                width: layout.width,
                height: layout.height,
                transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
              }}
            >
              <svg className="topology-edges" width={layout.width} height={layout.height} viewBox={`0 0 ${layout.width} ${layout.height}`} aria-hidden="true">
                {layout.edges.map((edge) => (
                  <path
                    key={`${edge.source.node.node_id}-${edge.target.node.node_id}`}
                    d={edge.path}
                    stroke={nodeStatusColor(edge.source.node.status)}
                  />
                ))}
              </svg>
              {layout.nodes.map((item) => {
                const node = item.node;
                const tokenRatio = node.budget.tokens_limit > 0
                  ? Math.min(100, node.budget.tokens_consumed / node.budget.tokens_limit * 100)
                  : 0;
                const activeNode = isRecentlyActive(node);
                return (
                  <article
                    className={`node-card ${nodeStatusClass(node.status)} ${selectedNode === node.node_id ? "selected" : ""} ${activeNode ? "is-active" : ""}`}
                    key={node.node_id}
                    style={{ left: item.x, top: item.y, width: NODE_WIDTH, height: NODE_HEIGHT, borderLeftColor: nodeStatusColor(node.status) }}
                    role="button"
                    tabIndex={0}
                    onPointerDown={(event) => event.stopPropagation()}
                    onClick={() => setSelectedNode(node.node_id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") setSelectedNode(node.node_id);
                    }}
                  >
                    <div className="node-card-top">
                      <div>
                        <span className="node-role-code">{node.role}</span>
                        <h3>{ROLE_LABELS[node.role] || node.role}</h3>
                      </div>
                      <span className="node-status" style={{ color: nodeStatusColor(node.status) }}><i /> {NODE_STATUS_LABELS[node.status]}</span>
                    </div>
                    <p className="node-model">{node.backend || "未接続"}{node.model ? ` / ${node.model}` : ""}</p>
                    <p className="node-activity">{node.activity}</p>
                    <div className="authority-line"><span>{node.authority.kind}</span>{node.authority.summary}</div>
                    <div className="budget-line">
                      <div><span style={{ width: `${tokenRatio}%` }} /></div>
                      <small>{Math.round(node.budget.tokens_consumed).toLocaleString()} / {Math.round(node.budget.tokens_limit).toLocaleString()} tokens</small>
                    </div>
                  </article>
                );
              })}
            </div>
          </div>

          <div className="topology-hint">カードを選択して詳細を開く · 空白をドラッグして移動 · ホイールでズーム</div>

          {selected && (
            <aside className="node-detail-panel" aria-label="選択したNodeの詳細">
              <div className="node-detail-heading">
                <div>
                  <span className="node-role-code">{selected.role}</span>
                  <h3>{ROLE_LABELS[selected.role] || selected.role}</h3>
                </div>
                <span className="node-status" style={{ color: nodeStatusColor(selected.status) }}><i /> {NODE_STATUS_LABELS[selected.status]}</span>
              </div>
              <p className="node-id-line">{selected.node_id}{selected.parent_id ? ` · 親: ${selected.parent_id}` : " · ルート"}</p>
              <div className="node-detail-metrics">
                <div><span>Backend / Model</span><strong>{selected.backend || "未接続"}{selected.model ? ` / ${selected.model}` : ""}</strong></div>
                <div><span>現在の活動</span><strong>{selected.activity}</strong></div>
                <div><span>権限・指示元</span><strong>{selected.authority.summary}{selected.authority.source ? ` · ${selected.authority.source}` : ""}</strong></div>
                <div><span>予算</span><strong>{Math.round(selected.budget.tokens_consumed).toLocaleString()} / {Math.round(selected.budget.tokens_limit).toLocaleString()} tokens · {selected.budget.wall_clock_seconds_consumed.toFixed(1)}s</strong></div>
              </div>

              <div className="node-detail-events">
                <span className="eyebrow">RECENT EVENTS</span>
                {selected.recent_events.length ? selected.recent_events.map((event, index) => (
                  <div className="node-event" key={event.event_id || `${event.event_type}-${event.seq || index}`}>
                    <i style={{ background: nodeStatusColor(selected.status) }} />
                    <div><strong>{event.summary}</strong><small>{event.ts ? formatDate(event.ts) : "時刻不明"} · {event.actor_type || "system"}</small></div>
                  </div>
                )) : <p className="detail-empty">イベント履歴はありません。</p>}
              </div>

              {active && (
                <div className="node-detail-actions">
                  <div className="node-action-buttons">
                    {selected.status === "SUSPENDED" ? (
                      <button disabled={busy} onClick={() => perform(
                        () => api.controlNode(runId, selected.node_id, "resume"),
                        () => setNodeStatus("RUNNING", "再開のackを受信"),
                      )}><Play size={14} /> 再開</button>
                    ) : (
                      <button disabled={busy || selected.status === "TERMINATED"} onClick={() => perform(
                        () => api.controlNode(runId, selected.node_id, "suspend"),
                        () => setNodeStatus("SUSPENDED", "休眠のackを受信"),
                      )}><Pause size={14} /> 休眠</button>
                    )}
                    {selected.terminable && <button disabled={busy} className="danger" onClick={() => perform(
                      () => api.controlNode(runId, selected.node_id, "terminate"),
                      () => setNodeStatus("TERMINATED", "停止のackを受信"),
                    )}><CircleStop size={14} /> 停止</button>}
                  </div>
                  <label className="detail-field"><span>このNodeへ追加指示</span><textarea value={instruction} onChange={(event) => setInstruction(event.target.value)} placeholder="Nodeに伝える具体的な指示" /></label>
                  <button className="primary-button" disabled={busy || !instruction.trim()} onClick={() => {
                    const message = instruction.trim();
                    void perform(
                      () => api.instruct(runId, message, selected.node_id),
                      (result) => updateTopology((current) => ({
                        ...current,
                        nodes: current.nodes.map((node) => node.node_id === selected.node_id
                          ? { ...node, activity: "指示を送信済み", authority: { kind: "instruction", id: (result as { instruction_id?: string }).instruction_id, summary: message, source: "human" } }
                          : node),
                      })),
                      () => setInstruction(""),
                    );
                  }}><Send size={15} /> 指示を送る</button>
                  <label className="detail-field"><span><OctagonAlert size={14} /> Algedonicを発信</span><textarea value={signal} onChange={(event) => setSignal(event.target.value)} placeholder="痛み・懸念・好機を組織へ通知" /></label>
                  <div className="split-actions">
                    <button disabled={busy || !signal.trim()} onClick={() => {
                      const message = signal.trim();
                      void perform(
                        () => api.algedonic(runId, "pain", message, selected.node_id),
                        () => updateTopology((current) => ({ ...current, nodes: current.nodes.map((node) => node.node_id === selected.node_id ? { ...node, activity: "痛覚のackを受信", authority: { kind: "algedonic", summary: message, source: "human" } } : node) })),
                        () => setSignal(""),
                      );
                    }}>痛覚を発信</button>
                    <button disabled={busy || !signal.trim()} onClick={() => {
                      const message = signal.trim();
                      void perform(
                        () => api.algedonic(runId, "pleasure", message, selected.node_id),
                        () => updateTopology((current) => ({ ...current, nodes: current.nodes.map((node) => node.node_id === selected.node_id ? { ...node, activity: "好機のackを受信", authority: { kind: "algedonic", summary: message, source: "human" } } : node) })),
                        () => setSignal(""),
                      );
                    }}>好機を発信</button>
                  </div>
                </div>
              )}
            </aside>
          )}

          {topology.waiting_consortiums.map((item) => (
            <div className="human-action" key={item.consortium_id}>
              <div><strong>合議体への意見</strong><p>{item.subject || item.consortium_id}</p></div>
              <input value={statement} onChange={(event) => setStatement(event.target.value)} placeholder="人間参加者としての意見" />
              <button disabled={busy || !statement.trim()} onClick={() => perform(
                () => api.consortiumStatement(item.consortium_id, statement),
                () => updateTopology((current) => ({ ...current, waiting_consortiums: current.waiting_consortiums.filter((consortium) => consortium.consortium_id !== item.consortium_id) })),
                () => setStatement(""),
              )}>投稿</button>
            </div>
          ))}
          {topology.pending_human_reviews.map((review) => (
            <div className="human-action" key={review.review_key}>
              <div><strong>Human review</strong><p>{review.subject} — {review.reason}</p></div>
              <input value={reviewResponse} onChange={(event) => setReviewResponse(event.target.value)} placeholder="判断・回答" />
              <button disabled={busy || !reviewResponse.trim()} onClick={() => perform(
                () => api.humanReview(runId, review.review_key, reviewResponse),
                () => updateTopology((current) => ({ ...current, pending_human_reviews: current.pending_human_reviews.filter((pending) => pending.review_key !== review.review_key) })),
                () => setReviewResponse(""),
              )}>回答</button>
            </div>
          ))}
        </>
      )}
    </section>
  );
}

function Timeline({ items }: { items: TimelineItem[] }) {
  const visible = useMemo(() => [...items].reverse(), [items]);
  if (visible.length === 0) {
    return <div className="timeline-empty">最初のイベントを待っています。</div>;
  }
  return (
    <div className="timeline">
      {visible.map((item) => (
        <details className={`timeline-item ${item.superseded ? "superseded" : ""}`} key={item.id}>
          <summary>
            <span className="timeline-dot" />
            <div className="timeline-main">
              <span className="timeline-system">{item.system} · G{item.generation}</span>
              <strong>{item.title}</strong>
              <p>{item.summary}</p>
            </div>
            <time>{item.ts ? formatDate(item.ts) : ""}</time>
          </summary>
          <div className="timeline-details">
            {item.superseded && <span className="superseded-label">差し替え済み</span>}
            <pre>{JSON.stringify(item.details, null, 2)}</pre>
          </div>
        </details>
      ))}
    </div>
  );
}

function Status({ status }: { status: RunStatus }) {
  return <span className={`status status-${status}`}><i /> {STATUS_LABELS[status]}</span>;
}

export default App;
