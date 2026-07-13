import { Check, Clipboard, GitPullRequest, LoaderCircle, Pause, Play, Plus, ShieldAlert, Square, X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import { api } from "./api";
import type { SelfDevProposalDetail, SelfDevProposalSummary } from "./types";

const STATES = [
  "PROPOSED", "CONSORTIUM_REVIEW", "APPROVED", "WORKSPACE_READY", "IMPLEMENTING",
  "GATES_RUNNING", "GATES_PASSED", "AUDIT", "FINAL_CONSORTIUM", "MERGE_READY", "DONE",
];

const STATE_LABELS: Record<string, string> = {
  PROPOSED: "提案済み", CONSORTIUM_REVIEW: "合議中", APPROVED: "承認済み",
  WORKSPACE_READY: "workspace準備済み", IMPLEMENTING: "実装中", GATES_RUNNING: "ゲート実行中",
  GATES_PASSED: "ゲート通過", AUDIT: "独立監査", FINAL_CONSORTIUM: "最終合議",
  MERGE_READY: "マージ準備完了", NEEDS_HUMAN: "人間判断待ち", GATES_FAILED: "ゲート失敗",
  REJECTED: "却下", REJECTED_FINAL: "最終却下", ABORTED: "中止", DONE: "完了", ARCHIVED: "保管",
};

function stateLabel(state: string) {
  return STATE_LABELS[state] || state;
}

function dateLabel(value: string) {
  return new Intl.DateTimeFormat("ja-JP", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

export function SelfDevView() {
  const [filter, setFilter] = useState<"all" | "human" | "MERGE_READY">("all");
  const [items, setItems] = useState<SelfDevProposalSummary[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<SelfDevProposalDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showForm, setShowForm] = useState(false);

  const refresh = async (keepSelection = true) => {
    setLoading(true);
    try {
      const params = filter === "human" ? { pendingAction: "human" } : filter === "MERGE_READY" ? { state: filter } : undefined;
      const next = await api.selfdevList(params);
      setItems(next.items);
      const preferred = keepSelection && selectedId && next.items.some((item) => item.proposal_id === selectedId)
        ? selectedId
        : next.items[0]?.proposal_id || "";
      setSelectedId(preferred);
      if (preferred) setDetail(await api.selfdevDetail(preferred));
      else setDetail(null);
      setError("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void refresh(false); }, [filter]);

  const select = async (proposalId: string) => {
    setSelectedId(proposalId);
    try {
      setDetail(await api.selfdevDetail(proposalId));
      setError("");
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <main className="selfdev-page">
      <section className="selfdev-header">
        <div>
          <p className="eyebrow">SELF-DEVELOPMENT LOOP</p>
          <h1><GitPullRequest size={29} /> 自己開発</h1>
          <p className="lede">提案、合議、実装、監査を一つの Proposal projection で追跡します。</p>
        </div>
        <button className="primary-button" onClick={() => setShowForm((value) => !value)}><Plus size={17} /> 新規Proposal</button>
      </section>

      {error && <div className="selfdev-error">{error}</div>}
      {showForm && <ProposalForm onCreated={async () => { setShowForm(false); await refresh(false); }} onError={setError} />}

      <div className="selfdev-layout">
        <section className="selfdev-list-panel">
          <div className="view-tabs selfdev-tabs">
            <button className={filter === "all" ? "active" : ""} onClick={() => setFilter("all")}>全件</button>
            <button className={filter === "human" ? "active" : ""} onClick={() => setFilter("human")}>承認待ち</button>
            <button className={filter === "MERGE_READY" ? "active" : ""} onClick={() => setFilter("MERGE_READY")}>MERGE_READY</button>
          </div>
          {loading ? <div className="selfdev-empty"><LoaderCircle className="spin" size={19} /> 読み込み中</div> : items.length === 0 ? (
            <div className="selfdev-empty"><GitPullRequest size={22} /> Proposal はありません。</div>
          ) : items.map((item) => (
            <button key={item.proposal_id} className={`selfdev-card ${selectedId === item.proposal_id ? "selected" : ""}`} onClick={() => void select(item.proposal_id)}>
              <div className="selfdev-card-top"><span className={`selfdev-state state-${item.state.toLowerCase()}`}>{stateLabel(item.state)}</span><time>{dateLabel(item.updated_at)}</time></div>
              <strong>{item.title}</strong>
              <div className="selfdev-card-meta"><span>{item.risk_class}</span><span>v{item.state_version}</span>{item.pause_causes.length > 0 && <span>pause {item.pause_causes.length}</span>}</div>
            </button>
          ))}
        </section>
        <section className="selfdev-detail-panel">
          {detail ? <ProposalDetail detail={detail} onChanged={() => void refresh()} onError={setError} /> : <div className="selfdev-empty">左の Proposal を選択してください。</div>}
        </section>
      </div>
    </main>
  );
}

function ProposalForm({ onCreated, onError }: { onCreated: () => Promise<void>; onError: (message: string) => void }) {
  const [title, setTitle] = useState("");
  const [motivation, setMotivation] = useState("");
  const [path, setPath] = useState("docs/");
  const [criterion, setCriterion] = useState("");
  const [risk, setRisk] = useState<"low" | "normal" | "protected">("normal");
  const [decisionRef, setDecisionRef] = useState("web-proposal");
  const [roadmapRef, setRoadmapRef] = useState("ROADMAP.md");
  const [tokens, setTokens] = useState("300000");
  const [seconds, setSeconds] = useState("7200");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!title.trim() || !motivation.trim() || !path.trim() || !criterion.trim() || busy) return;
    setBusy(true);
    try {
      await api.selfdevCreate({
        title: title.trim(), motivation: motivation.trim(),
        scope: [{ path: path.trim(), kind: "tree" }],
        acceptance_criteria: [{ id: "AC-1", statement: criterion.trim(), verifier: { kind: "path_exists", path: path.trim().replace(/\/$/, "") } }],
        risk_class: risk,
        budget_estimate: { tokens: Number(tokens), active_wall_clock_seconds: Number(seconds), pool_quota: [] },
        origin: { kind: "ready_queue", decision_ref: decisionRef.trim(), roadmap_ref: roadmapRef.trim() },
        dependencies: [],
      });
      await onCreated();
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="selfdev-form-panel">
      <div className="content-heading"><p className="eyebrow">NEW PROPOSAL</p><h2>改善候補を登録する</h2></div>
      <div className="selfdev-form-grid">
        <label>タイトル<input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例: docs の手順を一本化" /></label>
        <label>変更scope<input value={path} onChange={(event) => setPath(event.target.value)} placeholder="docs/setup.md" /></label>
        <label className="wide">動機<textarea value={motivation} onChange={(event) => setMotivation(event.target.value)} rows={3} /></label>
        <label className="wide">受入条件<textarea value={criterion} onChange={(event) => setCriterion(event.target.value)} rows={2} placeholder="検証可能な条件" /></label>
        <label>risk<select value={risk} onChange={(event) => setRisk(event.target.value as typeof risk)}><option value="low">low</option><option value="normal">normal</option><option value="protected">protected</option></select></label>
        <label>decision ref<input value={decisionRef} onChange={(event) => setDecisionRef(event.target.value)} /></label>
        <label>roadmap ref<input value={roadmapRef} onChange={(event) => setRoadmapRef(event.target.value)} /></label>
        <label>token見積<input type="number" min="0" value={tokens} onChange={(event) => setTokens(event.target.value)} /></label>
        <label>active秒見積<input type="number" min="0" value={seconds} onChange={(event) => setSeconds(event.target.value)} /></label>
      </div>
      <div className="selfdev-form-actions"><span>作成後のManifestはimmutableです。</span><button className="primary-button" onClick={() => void submit()} disabled={busy || !title.trim() || !path.trim() || !criterion.trim()}>{busy ? <LoaderCircle className="spin" size={17} /> : <Check size={17} />} 登録</button></div>
    </section>
  );
}

function ProposalDetail({ detail, onChanged, onError }: { detail: SelfDevProposalDetail; onChanged: () => void; onError: (message: string) => void }) {
  const [reason, setReason] = useState("");
  const [statement, setStatement] = useState("");
  const [selectedPauseId, setSelectedPauseId] = useState("");
  const [busy, setBusy] = useState(false);

  const run = async (operation: () => Promise<unknown>) => {
    setBusy(true);
    try { await operation(); setReason(""); setStatement(""); onChanged(); } catch (err) { onError((err as Error).message); } finally { setBusy(false); }
  };
  const human = (decision: "approve" | "reject" | "respond") => run(() => api.selfdevHumanDecision(detail.proposal.id, {
    decision, reason: reason.trim(), statement: decision === "respond" ? statement.trim() : null,
    expected_state_version: detail.state_version,
    ...(decision === "approve" ? { proposal_manifest_sha256: detail.proposal_manifest_sha256, protected_scope_sha256: detail.protected_scope_sha256 } : {}),
  }));
  const effectDecision = (effectId: string, decision: "completed" | "failed") => run(() => api.selfdevHumanDecision(detail.proposal.id, {
    decision,
    effect_id: effectId,
    reason: reason.trim(),
    expected_state_version: detail.state_version,
  }));
  const control = (action: "suspend" | "resume" | "abort" | "force_abort") => run(() => api.selfdevControl(
    detail.proposal.id,
    action,
    reason.trim(),
    detail.state_version,
    action === "resume" ? selectedPauseId || (detail.pause_causes.length === 1 ? detail.pause_causes[0].pause_id : undefined) : undefined,
  ));

  return (
    <article className="selfdev-detail">
      <div className="selfdev-detail-head"><div><p className="eyebrow">PROPOSAL DETAIL</p><h2>{detail.proposal.title}</h2><p>{detail.proposal.motivation}</p></div><span className={`selfdev-state large state-${detail.state.toLowerCase()}`}>{stateLabel(detail.state)}</span></div>
      <div className="state-rail">{STATES.map((state) => <span key={state} className={state === detail.state ? "current" : STATES.indexOf(state) < STATES.indexOf(detail.state) ? "passed" : ""}>{stateLabel(state)}</span>)}</div>
      {detail.pause_causes.map((cause) => <div className="pause-notice" key={cause.pause_id}><Pause size={16} /><span><strong>{cause.kind}</strong> {cause.reason}{cause.reset_at ? `（${cause.reset_at} 復帰予定）` : ""}</span></div>)}

      {detail.in_doubt_effects.length > 0 && <section className="selfdev-action-card human effect-resolution" aria-label="in-doubt効果の裁定"><div><h3><ShieldAlert size={17} /> in-doubt 効果の人間裁定</h3><p>外部事実を確認し、completed（存在する）または failed（存在しない）を理由付きで記録します。</p></div>{detail.in_doubt_effects.map((effect) => <div className="effect-row" key={effect.effect_id}><code>{effect.effect_id}</code><span>{effect.effect_kind} · {effect.input_sha256.slice(0, 12)}… · {dateLabel(effect.invoked_at)}</span><div className="selfdev-action-buttons"><button disabled={busy || !reason.trim()} onClick={() => void effectDecision(effect.effect_id, "completed")}>completed</button><button className="danger" disabled={busy || !reason.trim()} onClick={() => void effectDecision(effect.effect_id, "failed")}>failed</button></div></div>)}</section>}

      {detail.state === "NEEDS_HUMAN" && <section className="selfdev-action-card human"><div><h3><ShieldAlert size={17} /> Human の判断</h3><p>合議の内容を確認し、承認・却下・追加 statement を記録します。</p></div><textarea value={statement || reason} onChange={(event) => { setStatement(event.target.value); setReason(event.target.value); }} placeholder="理由または statement" rows={2} /><div className="selfdev-action-buttons"><button disabled={busy} onClick={() => void human("respond")}>statementを送る</button><button disabled={busy} onClick={() => void human("approve")}>承認</button><button className="danger" disabled={busy} onClick={() => void human("reject")}>却下</button></div></section>}
      {detail.state !== "NEEDS_HUMAN" && !["DONE", "ARCHIVED", "REJECTED", "ABORTED", "REJECTED_FINAL"].includes(detail.state) && <section className="selfdev-action-card"><h3>介入</h3><input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="操作理由（裁定・操作に必須）" />{detail.pause_causes.length > 1 && <label>resume対象<select value={selectedPauseId} onChange={(event) => setSelectedPauseId(event.target.value)}><option value="">pauseを選択</option>{detail.pause_causes.map((cause) => <option key={cause.pause_id} value={cause.pause_id}>{cause.pause_id} · {cause.kind}</option>)}</select></label>}<div className="selfdev-action-buttons"><button disabled={busy || !reason.trim()} onClick={() => void control("suspend")}><Pause size={14} /> suspend</button><button disabled={busy || !reason.trim() || (detail.pause_causes.length > 1 && !selectedPauseId)} onClick={() => void control("resume")}><Play size={14} /> resume</button><button className="danger" disabled={busy || !reason.trim()} onClick={() => void control("abort")}><Square size={14} /> abort</button>{detail.in_doubt_effects.length === 0 && <button className="danger" disabled={busy || !reason.trim()} onClick={() => void control("force_abort")}><Square size={14} /> force abort</button>}</div></section>}

      <section className="selfdev-info-grid">
        <Info title="ProposalManifest"><pre>{JSON.stringify(detail.proposal, null, 2)}</pre></Info>
        <Info title="状態遷移履歴"><div className="selfdev-history">{detail.transitions.map((item) => <div key={item.event_id}><strong>{item.transition.to_state as string}</strong><span>{item.transition.reason as string}</span><time>{item.ts}</time></div>)}</div></Info>
        <Info title="Consortium決定全文">{detail.consortium_reviews.length ? detail.consortium_reviews.map((item, index) => <pre key={index}>{JSON.stringify(item, null, 2)}</pre>) : <p className="detail-empty">未提出</p>}</Info>
        <Info title="Gate report">{detail.gate_attempts.length ? detail.gate_attempts.map((item, index) => <pre key={index}>{JSON.stringify(item.report || item, null, 2)}</pre>) : <p className="detail-empty">未実行</p>}</Info>
        <Info title="S3★独立監査"><pre>{detail.audit_report ? JSON.stringify(detail.audit_report, null, 2) : "未提出"}</pre></Info>
        <Info title="予算見積と実績"><pre>{JSON.stringify({ estimate: detail.proposal.budget_estimate, actual: detail.budget_actual }, null, 2)}</pre></Info>
        <Info title="候補 branch / commit"><pre>{JSON.stringify(detail.candidate || "未作成", null, 2)}</pre></Info>
        <Info title="成果物"><div className="artifact-list">{detail.artifacts.map((artifact) => <a key={artifact.name} href={api.selfdevArtifactUrl(detail.proposal.id, artifact.name)} target="_blank" rel="noreferrer">{artifact.name}<small>{artifact.kind}</small></a>)}</div></Info>
      </section>

      {detail.pr_description && <section className="selfdev-pr-panel"><div className="content-heading"><p className="eyebrow">PR DESCRIPTION</p><h3>人間のPR説明文</h3></div><div className="selfdev-pr-actions"><button disabled={detail.state !== "MERGE_READY"} onClick={() => navigator.clipboard.writeText(detail.pr_description || "")}><Clipboard size={15} /> コピー</button>{detail.state === "MERGE_READY" && <><button disabled={busy || !reason.trim()} onClick={() => void run(() => api.selfdevMergeOutcome(detail.proposal.id, true, reason.trim()))}>merge済みを記録</button><button disabled={busy || !reason.trim()} onClick={() => void run(() => api.selfdevMergeOutcome(detail.proposal.id, false, reason.trim()))}>候補を保管</button></>}</div><ReactMarkdown>{detail.pr_description}</ReactMarkdown></section>}
      {detail.last_error && <div className="selfdev-error">{detail.last_error}</div>}
    </article>
  );
}

function Info({ title, children }: { title: string; children: ReactNode }) {
  return <section className="selfdev-info-card"><h3>{title}</h3>{children}</section>;
}
