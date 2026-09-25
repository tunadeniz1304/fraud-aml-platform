import { useEffect, useState } from "react";
import { api, sse, tl } from "../api";
import { Card, DecisionBadge, Empty, RiskBar, Stat } from "../components/ui";

type LiveEvent = {
  transaction_id: string;
  customer_id: string;
  ts: string;
  amount_try: number;
  decision: string;
  risk_score: number;
  latency_ms: number;
  channel?: string;
  country?: string;
  reason?: string;
  ring_id?: string;
};
type Summary = {
  tps_1m: number;
  latency_ms: { p50: number | null; p95: number | null; p99: number | null };
  decisions: Record<string, number>;
  open_cases: Record<string, number>;
  llm_mode: string;
};

/** Memory cap for the SSE buffer and the number of rows rendered. */
export const LIVE_CAP = 150;
export const LIVE_RENDER = 100;

export default function LivePage() {
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const stop = sse("/api/live/stream", (type, data) => {
      if (type === "ready") setConnected(true);
      if (type === "decision") setEvents((prev) => [data as LiveEvent, ...prev].slice(0, LIVE_CAP));
    });
    const load = () => api<Summary>("/api/live/summary").then(setSummary).catch(() => undefined);
    load();
    const timer = window.setInterval(load, 3000);
    return () => {
      stop();
      window.clearInterval(timer);
    };
  }, []);

  const openCases = summary ? ["YENI", "INCELENIYOR", "BEKLEMEDE"].reduce((n, k) => n + (summary.open_cases[k] ?? 0), 0) : 0;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat label="İşlem / sn (1 dk)" value={summary?.tps_1m.toFixed(2) ?? "—"} />
        <Stat label="Skor p50" value={summary?.latency_ms.p50 != null ? `${summary.latency_ms.p50} ms` : "—"} />
        <Stat label="Skor p99" value={summary?.latency_ms.p99 != null ? `${summary.latency_ms.p99} ms` : "—"} hint="hedef < 50 ms" />
        <Stat label="Açık vaka" value={openCases} />
        <Stat label="Karar dağılımı" value={<span className="text-sm">{Object.entries(summary?.decisions ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"}</span>} />
      </div>
      <Card title="Canlı işlem akışı" actions={<span role="status" className={connected ? "text-xs text-emerald-700 dark:text-emerald-400" : "text-xs text-slate-600 dark:text-slate-400"}>{connected ? "● bağlı (SSE)" : "○ bağlanıyor…"}</span>}>
        {events.length === 0 ? (
          <Empty>Akış bekleniyor — simülatör çalışıyorsa işlemler burada belirir. “Senaryo” sekmesinden saldırı tetikleyebilirsiniz.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table>
              <caption className="mb-1 text-left text-xs text-slate-600 dark:text-slate-400">
                Son {Math.min(events.length, LIVE_RENDER)} işlem gösteriliyor (bellekte en fazla {LIVE_CAP}, ekranda en fazla {LIVE_RENDER} satır tutulur).
              </caption>
              <thead>
                <tr><th scope="col">İşlem</th><th scope="col">Müşteri</th><th scope="col">Tutar</th><th scope="col">Karar</th><th scope="col">Risk</th><th scope="col">Neden</th><th scope="col">Gecikme (ms)</th></tr>
              </thead>
              <tbody>
                {events.slice(0, LIVE_RENDER).map((e) => (
                  <tr key={e.transaction_id}>
                    <td className="font-mono text-xs">{e.transaction_id}</td>
                    <td>{e.customer_id}{e.ring_id && <span className="ml-1 text-xs text-rose-700 dark:text-rose-400">{e.ring_id}</span>}</td>
                    <td className="tabular-nums">{tl(e.amount_try)}</td>
                    <td><DecisionBadge decision={e.decision} /></td>
                    <td><RiskBar value={e.risk_score ?? 0} /></td>
                    <td className="max-w-md truncate text-xs text-slate-600 dark:text-slate-400" title={e.reason ?? ""}>{e.reason ?? ""}</td>
                    <td className="tabular-nums text-xs">{e.latency_ms?.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
