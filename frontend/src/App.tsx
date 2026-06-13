import {
  ArrowLeft,
  ArrowUpRight,
  CircleStop,
  FileText,
  History,
  LoaderCircle,
  Paperclip,
  Plus,
  RotateCcw,
  Send,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { api } from "./api";
import type { AppConfig, RunDetail, RunStatus, RunSummary, TimelineItem } from "./types";

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
  const [files, setFiles] = useState<File[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const activeRun = runs.find((run) => ["queued", "running", "interrupting", "waiting_for_user"].includes(run.status));

  const addFiles = (incoming: FileList | null) => {
    if (!incoming) return;
    setFiles((current) => [...current, ...Array.from(incoming)].slice(0, 10));
  };

  const submit = async () => {
    if (!description.trim() || submitting) return;
    setSubmitting(true);
    onError("");
    try {
      onCreated(await api.createRun(description, files));
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
          {files.length > 0 && (
            <div className="file-list">
              {files.map((file, index) => (
                <span className="file-chip" key={`${file.name}-${index}`}>
                  <FileText size={14} /> {file.name}
                  <button onClick={() => setFiles((current) => current.filter((_, i) => i !== index))}>
                    <X size={13} />
                  </button>
                </span>
              ))}
            </div>
          )}
          <div className="composer-footer">
            <input
              ref={fileInput}
              type="file"
              multiple
              accept=".txt,.md,.json,.csv,.pdf,.png,.jpg,.jpeg,.webp"
              onChange={(event) => addFiles(event.target.files)}
              hidden
            />
            <button className="attach-button" onClick={() => fileInput.current?.click()} disabled={Boolean(activeRun)}>
              <Paperclip size={17} /> ファイル
            </button>
            <span className="composer-hint">最大10件 / 合計50 MB</span>
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
            <p>現在の処理を中断し、最新の指示を優先してこの工程から組み直します。</p>
          </div>
          <div className="instruction-row">
            <input
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              placeholder="例: 実装速度より保守性を優先して"
              onKeyDown={(event) => {
                if (event.key === "Enter" && instruction.trim()) {
                  action(() => api.interrupt(run.run_id, instruction)).then(() => setInstruction(""));
                }
              }}
            />
            <button
              className="secondary-button"
              disabled={!instruction.trim() || busy}
              onClick={() => action(() => api.interrupt(run.run_id, instruction)).then(() => setInstruction(""))}
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
