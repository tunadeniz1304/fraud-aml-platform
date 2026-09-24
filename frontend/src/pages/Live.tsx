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

export default function LivePage() {
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const stop = sse("/api/live/stream", (type, data) => {
      if (type === "ready") setConnected(true);
      if (type === "decision") setEvents((prev) => [data as LiveEvent, ...prev].slice(0, 150));
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
      <Card title="Canlı işlem akışı" actions={<span className={connected ? "text-xs text-emerald-600" : "text-xs text-slate-400"}>{connected ? "● bağlı (SSE)" : "○ bağlanıyor…"}</span>}>
        {events.length === 0 ? (
          <Empty>Akış bekleniyor — simülatör çalışıyorsa işlemler burada belirir. “Senaryo” sekmesinden saldırı tetikleyebilirsiniz.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table>
              <thead>
                <tr><th>İşlem</th><th>Müşteri</th><th>Tutar</th><th>Karar</th><th>Risk</th><th>Neden</th><th>ms</th></tr>
              </thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.transaction_id}>
                    <td className="font-mono text-xs">{e.transaction_id}</td>
                    <td>{e.customer_id}{e.ring_id && <span className="ml-1 text-xs text-rose-500">{e.ring_id}</span>}</td>
                    <td className="tabular-nums">{tl(e.amount_try)}</td>
                    <td><DecisionBadge decision={e.decision} /></td>
                    <td><RiskBar value={e.risk_score ?? 0} /></td>
                    <td className="max-w-md truncate text-xs text-slate-500" title={e.reason ?? ""}>{e.reason ?? ""}</td>
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
