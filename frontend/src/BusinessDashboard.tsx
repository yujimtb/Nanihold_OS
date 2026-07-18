import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Activity, AlertTriangle, Banknote, Gauge, LoaderCircle, ShieldCheck } from "lucide-react";
import { api } from "./api";
import type { SurvivalDashboard as SurvivalDashboardData } from "./types";

function yen(value: number | null) {
  if (value === null) return "未算出";
  return `¥${value.toLocaleString("ja-JP")}`;
}

function ratio(value: number | null) {
  return value === null ? "未算出" : value.toFixed(2);
}

export function BusinessDashboard() {
  const [dashboard, setDashboard] = useState<SurvivalDashboardData | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setLoading(true);
    api.survivalDashboard()
      .then((value) => { if (active) setDashboard(value); })
      .catch((reason: Error) => { if (active) setError(reason.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  const maxFlow = useMemo(() => {
    if (!dashboard) return 1;
    return Math.max(
      1,
      ...dashboard.daily_trend.flatMap((day) => [day.revenue_jpy, day.expense_jpy]),
    );
  }, [dashboard]);

  if (loading) {
    return <main className="business-page business-loading"><LoaderCircle className="spin" /> 事業状況を読み込んでいます</main>;
  }
  if (error || !dashboard) {
    return <main className="business-page"><div className="business-error"><AlertTriangle size={18} /> {error || "事業状況を取得できませんでした"}</div></main>;
  }

  const report = dashboard.report;
  return (
    <main className="business-page">
      <section className="business-header">
        <div>
          <p className="eyebrow">BUSINESS SURVIVAL</p>
          <h1>事業状況</h1>
          <p className="lede">円建て台帳から、資金・原価・runwayの現在地を確認します。</p>
        </div>
        <div className="business-as-of">基準日 {report.report_date}</div>
      </section>

      <section className="business-metrics" aria-label="主要指標">
        <Metric icon={<Banknote size={17} />} label="Available cash" value={yen(report.available_cash)} />
        <Metric icon={<Gauge size={17} />} label="Runway" value={report.runway_months === null ? "未算出" : `${report.runway_months.toFixed(1)}ヶ月`} hint={report.runway_reason} />
        <Metric icon={<Activity size={17} />} label="Burn / 30d" value={yen(report.burn_30d_cash)} />
        <Metric icon={<ShieldCheck size={17} />} label="R cash / economic" value={`${ratio(report.R_cash)} / ${ratio(report.R_economic)}`} />
      </section>

      <section className="business-grid">
        <article className="business-panel business-trend-panel">
          <div className="section-heading"><div><p className="eyebrow">DAILY TREND</p><h2>収支の日次推移</h2></div><span>直近30日</span></div>
          <div className="business-chart" aria-label="収支の日次推移">
            {dashboard.daily_trend.map((day) => (
              <div className="business-chart-day" key={day.date} title={`${day.date} 売上 ${yen(day.revenue_jpy)} / 支出 ${yen(day.expense_jpy)}`}>
                <span className="business-bar revenue" style={{ height: `${Math.max(2, day.revenue_jpy / maxFlow * 100)}%` }} />
                <span className="business-bar expense" style={{ height: `${Math.max(2, day.expense_jpy / maxFlow * 100)}%` }} />
              </div>
            ))}
          </div>
          <div className="business-legend"><span><i className="revenue-dot" />売上</span><span><i className="expense-dot" />支出</span></div>
        </article>

        <article className="business-panel">
          <div className="section-heading"><div><p className="eyebrow">MEASUREMENT STATUS</p><h2>計測状態</h2></div></div>
          <dl className="business-facts">
            <div><dt>Ledger entries</dt><dd>{dashboard.ledger.entry_count}</dd></div>
            <div><dt>Usage records</dt><dd>{dashboard.ledger.usage_count}</dd></div>
            <div><dt>未価格化 usage</dt><dd>{report.unpriced_usage.count}件 / {report.unpriced_usage.tokens.toLocaleString("ja-JP")} tokens</dd></div>
            <div><dt>Owner dependency</dt><dd>{yen(report.owner_dependency)}</dd></div>
          </dl>
          <div className="business-safety"><ShieldCheck size={16} /><span>loopback: {dashboard.safety.bind_host} / 外部送信: 無効 / 実請求: 無効</span></div>
          <p className="business-placeholder">Human認証: {dashboard.safety.human_auth_status}（Wave 0では未実装）</p>
        </article>
      </section>
    </main>
  );
}

function Metric({ icon, label, value, hint }: { icon: ReactNode; label: string; value: string; hint?: string | null }) {
  return <div className="business-metric"><span className="business-metric-icon">{icon}</span><span className="eyebrow">{label}</span><strong>{value}</strong>{hint && <small>{hint}</small>}</div>;
}
