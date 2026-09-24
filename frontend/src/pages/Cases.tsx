import { useEffect, useState } from "react";
import { api, tl, when } from "../api";
import { Badge, Button, Card, Empty, ErrorNote } from "../components/ui";

export type CaseRow = {
  id: number;
  customer_id: string;
  case_type: string;
  title: string;
  status: string;
  priority: number;
  assigned_to: string | null;
  ring_id: string | null;
  alert_count: number;
  total_amount_try: number;
  internal_sla_due: string | null;
  internal_sla_breached: boolean;
  sla_remaining_s: number | null;
  masak_deadline: string | null;
  masak_business_days_left: number | null;
  sib_status: string | null;
};

function slaText(row: CaseRow) {
  if (row.sla_remaining_s == null) return "—";
  if (row.sla_remaining_s < 0) return "SLA aşıldı";
  const h = Math.floor(row.sla_remaining_s / 3600);
  const m = Math.floor((row.sla_remaining_s % 3600) / 60);
  return `${h} sa ${m} dk`;
}

export default function CasesPage() {
  const [rows, setRows] = useState<CaseRow[]>([]);
  const [status, setStatus] = useState("OPEN");
  const [order, setOrder] = useState("priority");
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    api<CaseRow[]>(`/api/cases?status=${status}&order=${order}&limit=200`).then(setRows).catch((e) => setError(e.message));
  useEffect(() => {
    load();
    const t = window.setInterval(load, 10000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, order]);

  const assign = async (id: number) => {
    try {
      await api(`/api/cases/${id}/assign`, { method: "POST", json: {} });
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <Card
      title="Alert / vaka kuyruğu"
      actions={
        <div className="flex flex-wrap gap-2 text-sm">
          <label>Durum{" "}
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              {["OPEN", "YENI", "INCELENIYOR", "BEKLEMEDE", "KAPANDI_FRAUD", "KAPANDI_TEMIZ", "SIB_GONDERILDI"].map((s) => <option key={s}>{s}</option>)}
            </select>
          </label>
          <label>Sıralama{" "}
            <select value={order} onChange={(e) => setOrder(e.target.value)}>
              <option value="priority">öncelik (risk × tutar)</option>
              <option value="sla">iç SLA</option>
              <option value="masak">MASAK süresi</option>
              <option value="recent">son güncellenen</option>
            </select>
          </label>
        </div>
      }
    >
      <ErrorNote error={error} />
      {rows.length === 0 ? <Empty>Kuyruk boş.</Empty> : (
        <div className="overflow-x-auto">
          <table>
            <thead>
              <tr><th>#</th><th>Vaka</th><th>Durum</th><th>Öncelik</th><th>Tutar</th><th>İç SLA</th><th>MASAK</th><th>Atanan</th><th /></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className={r.internal_sla_breached ? "bg-rose-50 dark:bg-rose-950/30" : ""}>
                  <td className="font-mono">{r.id}</td>
                  <td>
                    <a className="font-medium text-indigo-600 hover:underline dark:text-indigo-400" href={`#/cases/${r.id}`}>{r.title}</a>
                    <div className="mt-1 flex gap-1"><Badge tone="indigo">{r.case_type}</Badge>{r.ring_id && <Badge tone="rose">{r.ring_id}</Badge>}{r.sib_status && <Badge tone="amber">{r.sib_status}</Badge>}<Badge>{r.alert_count} alert</Badge></div>
                  </td>
                  <td>{r.status}</td>
                  <td className="tabular-nums">{Math.round(r.priority).toLocaleString("tr-TR")}</td>
                  <td className="tabular-nums">{tl(r.total_amount_try)}</td>
                  <td className={r.internal_sla_breached ? "font-semibold text-rose-600" : ""}>{slaText(r)}</td>
                  <td title={when(r.masak_deadline)}>{r.masak_business_days_left ?? "—"} iş günü</td>
                  <td>{r.assigned_to ?? "—"}</td>
                  <td>{!r.assigned_to && <Button variant="ghost" onClick={() => assign(r.id)}>Üstlen</Button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
