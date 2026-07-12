import {
  ArrowLeft,
  ArrowUpRight,
  CircleStop,
  FileText,
  History,
  LoaderCircle,
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
import { useCallback, useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import { api } from "./api";
import type { AppConfig, RunDetail, RunStatus, RunSummary, TimelineItem, Topology, TopologyNode } from "./types";

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
    setRuns((current) => [detail, ...current.filter((run) => run.run_id !== detail.run_id)]);
  };

  const handleDelete = async (runId: string) => {
    if (!window.confirm("このRunのログ、添付、結果をすべて削除しますか？")) return;
    await api.delete(runId);
    setSelected(null);
    await refreshRuns();
  };

  if (loading) {
    return <div className="boot"><LoaderCircle className="spin" /> Nanihold OSを起動しています</div>;
  }

  return (
    <div className="shell">
      <header className="topbar">
        <button className="brand" onClick={() => setSelected(null)}>
          <span className="brand-mark">N</span>
          <span>Nanihold OS</span>
        </button>
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

      {selected ? (
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
              デモモデルで起動中です。実モデルは <code>.env</code> で設定できます。
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

      <OrganizationView runId={run.run_id} active={active} />

      <div className="run-content">
        <section className="result-column">
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

        <section className="timeline-column">
          <div className="content-heading">
            <p className="eyebrow">PROCESS LOG</p>
            <h2>処理過程</h2>
          </div>
          <Timeline items={run.timeline} />
        </section>
      </div>

      {!active && (
        <div className="danger-zone">
          <button onClick={onDelete}><Trash2 size={16} /> このRunを削除</button>
        </div>
      )}
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

function OrganizationView({ runId, active }: { runId: string; active: boolean }) {
  const [topology, setTopology] = useState<Topology | null>(null);
  const [selectedNode, setSelectedNode] = useState<string>("");
  const [instruction, setInstruction] = useState("");
  const [signal, setSignal] = useState("");
  const [statement, setStatement] = useState("");
  const [reviewResponse, setReviewResponse] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const next = await api.topology(runId);
      setTopology(next);
      if (!selectedNode && next.nodes.length) setSelectedNode(next.nodes[0].node_id);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [runId, selectedNode]);

  useEffect(() => {
    refresh();
    if (!active) return;
    const timer = window.setInterval(refresh, 1500);
    return () => window.clearInterval(timer);
  }, [active, refresh]);

  const perform = async (operation: () => Promise<unknown>, clear?: () => void) => {
    setBusy(true);
    setError("");
    try {
      await operation();
      clear?.();
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const depthOf = (node: TopologyNode) => {
    let depth = 0;
    let parent = node.parent_id;
    while (parent && depth < 8) {
      depth += 1;
      parent = topology?.nodes.find((item) => item.node_id === parent)?.parent_id ?? null;
    }
    return depth;
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
      {!topology ? (
        <div className="topology-loading"><LoaderCircle className="spin" /> 組織を再構成しています</div>
      ) : (
        <>
          <div className="node-tree">
            {topology.nodes.map((node) => {
              const tokenRatio = node.budget.tokens_limit > 0
                ? Math.min(100, node.budget.tokens_consumed / node.budget.tokens_limit * 100)
                : 0;
              return (
                <article
                  className={`node-card node-${node.status.toLowerCase()} ${selectedNode === node.node_id ? "selected" : ""}`}
                  key={node.node_id}
                  style={{ marginLeft: `${depthOf(node) * 34}px` }}
                  onClick={() => setSelectedNode(node.node_id)}
                >
                  <div className="node-card-top">
                    <div>
                      <span className="node-role-code">{node.role}</span>
                      <h3>{ROLE_LABELS[node.role] || node.role}</h3>
                    </div>
                    <span className="node-status"><i /> {node.status}</span>
                  </div>
                  <p className="node-model">{node.backend || "未接続"}{node.model ? ` / ${node.model}` : ""}</p>
                  <p className="node-activity">{node.activity}</p>
                  <div className="authority-line"><span>{node.authority.kind}</span>{node.authority.summary}</div>
                  <div className="budget-line">
                    <div><span style={{ width: `${tokenRatio}%` }} /></div>
                    <small>{Math.round(node.budget.tokens_consumed).toLocaleString()} / {Math.round(node.budget.tokens_limit).toLocaleString()} tokens</small>
                  </div>
                  {active && selectedNode === node.node_id && (
                    <div className="node-actions" onClick={(event) => event.stopPropagation()}>
                      {node.status === "SUSPENDED" ? (
                        <button disabled={busy} onClick={() => perform(() => api.controlNode(runId, node.node_id, "resume"))}><Play size={14} /> 再開</button>
                      ) : (
                        <button disabled={busy} onClick={() => perform(() => api.controlNode(runId, node.node_id, "suspend"))}><Pause size={14} /> 休眠</button>
                      )}
                      {node.terminable && <button disabled={busy} className="danger" onClick={() => perform(() => api.controlNode(runId, node.node_id, "terminate"))}><CircleStop size={14} /> 停止</button>}
                    </div>
                  )}
                </article>
              );
            })}
          </div>

          {active && (
            <div className="organization-controls">
              <div className="control-card">
                <h3>Node へ追加指示</h3>
                <select value={selectedNode} onChange={(event) => setSelectedNode(event.target.value)}>
                  {topology.nodes.map((node) => <option key={node.node_id} value={node.node_id}>{ROLE_LABELS[node.role] || node.role}</option>)}
                </select>
                <textarea value={instruction} onChange={(event) => setInstruction(event.target.value)} placeholder="この Node に伝える具体的な指示" />
                <button disabled={busy || !instruction.trim()} onClick={() => perform(() => api.instruct(runId, instruction, selectedNode), () => setInstruction(""))}><Send size={15} /> 指示を送る</button>
              </div>
              <div className="control-card alert-card">
                <h3><OctagonAlert size={16} /> Algedonic</h3>
                <textarea value={signal} onChange={(event) => setSignal(event.target.value)} placeholder="組織全体へ即時通知する痛み・懸念" />
                <div className="split-actions">
                  <button disabled={busy || !signal.trim() || !selectedNode} onClick={() => perform(() => api.algedonic(runId, "pain", signal, selectedNode), () => setSignal(""))}>痛覚を発信</button>
                  <button disabled={busy || !signal.trim() || !selectedNode} onClick={() => perform(() => api.algedonic(runId, "pleasure", signal, selectedNode), () => setSignal(""))}>好機を発信</button>
                </div>
              </div>
            </div>
          )}

          {topology.waiting_consortiums.map((item) => (
            <div className="human-action" key={item.consortium_id}>
              <div><strong>合議体への意見</strong><p>{item.subject || item.consortium_id}</p></div>
              <input value={statement} onChange={(event) => setStatement(event.target.value)} placeholder="人間参加者としての意見" />
              <button disabled={busy || !statement.trim()} onClick={() => perform(() => api.consortiumStatement(item.consortium_id, statement), () => setStatement(""))}>投稿</button>
            </div>
          ))}
          {topology.pending_human_reviews.map((review) => (
            <div className="human-action" key={review.review_key}>
              <div><strong>Human review</strong><p>{review.subject} — {review.reason}</p></div>
              <input value={reviewResponse} onChange={(event) => setReviewResponse(event.target.value)} placeholder="判断・回答" />
              <button disabled={busy || !reviewResponse.trim()} onClick={() => perform(() => api.humanReview(runId, review.review_key, reviewResponse), () => setReviewResponse(""))}>回答</button>
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
