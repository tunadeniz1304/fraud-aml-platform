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

export const PAGE_SIZE = 25;
const STATUSES = ["OPEN", "YENI", "INCELENIYOR", "BEKLEMEDE", "KAPANDI_FRAUD", "KAPANDI_TEMIZ", "SIB_GONDERILDI"];
const CASE_TYPES = ["ATO", "APP", "MULE", "AML", "CARD_TESTING", "YAPTIRIM", "DAVRANIS"];
type Page = { items: CaseRow[]; total: number; limit: number; offset: number };

export default function CasesPage() {
  const [rows, setRows] = useState<CaseRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState("OPEN");
  const [caseType, setCaseType] = useState("");
  const [order, setOrder] = useState("priority");
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    const q = new URLSearchParams({ order, limit: String(PAGE_SIZE), offset: String(offset) });
    if (status) q.set("status", status);
    if (caseType) q.set("case_type", caseType);
    return api<Page>(`/api/cases/page?${q.toString()}`)
      .then((p) => {
        setRows(p.items);
        setTotal(p.total);
      })
      .catch((e) => setError(e.message));
  };
  useEffect(() => {
    load();
    const t = window.setInterval(load, 10000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, caseType, order, offset]);

  const filter = (set: (v: string) => void) => (e: React.ChangeEvent<HTMLSelectElement>) => {
    set(e.target.value);
    setOffset(0);
  };

  const assign = async (id: number) => {
    try {
      await api(`/api/cases/${id}/assign`, { method: "POST", json: {} });
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + rows.length, total);
  const pager = (
    <nav className="mt-3 flex items-center justify-end gap-2 text-sm" aria-label="Sayfalama">
      <Button variant="ghost" onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} disabled={offset === 0} aria-label="Önceki sayfa">‹ Önceki</Button>
      <span aria-live="polite" className="tabular-nums">{from}–{to} / {total}</span>
      <Button variant="ghost" onClick={() => setOffset(offset + PAGE_SIZE)} disabled={offset + PAGE_SIZE >= total} aria-label="Sonraki sayfa">Sonraki ›</Button>
    </nav>
  );

  return (
    <Card
      title="Alert / vaka kuyruğu"
      actions={
        <div className="flex flex-wrap gap-2 text-sm">
          <label>Durum{" "}
            <select value={status} onChange={filter(setStatus)}>
              {STATUSES.map((s) => <option key={s}>{s}</option>)}
            </select>
          </label>
          <label>Vaka türü{" "}
            <select value={caseType} onChange={filter(setCaseType)}>
              <option value="">tümü</option>
              {CASE_TYPES.map((s) => <option key={s}>{s}</option>)}
            </select>
          </label>
          <label>Sıralama{" "}
            <select value={order} onChange={filter(setOrder)}>
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
            <caption className="sr-only">Vaka kuyruğu</caption>
            <thead>
              <tr><th scope="col">#</th><th scope="col">Vaka</th><th scope="col">Durum</th><th scope="col">Öncelik</th><th scope="col">Tutar</th><th scope="col">İç SLA</th><th scope="col">MASAK</th><th scope="col">Atanan</th><th scope="col"><span className="sr-only">İşlem</span></th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className={r.internal_sla_breached ? "bg-rose-50 dark:bg-rose-950/30" : ""}>
                  <td className="font-mono">{r.id}</td>
                  <td>
                    <a className="font-medium text-indigo-700 dark:text-indigo-300 hover:underline dark:text-indigo-400" href={`#/cases/${r.id}`}>{r.title}</a>
                    <div className="mt-1 flex gap-1"><Badge tone="indigo">{r.case_type}</Badge>{r.ring_id && <Badge tone="rose">{r.ring_id}</Badge>}{r.sib_status && <Badge tone="amber">{r.sib_status}</Badge>}<Badge>{r.alert_count} alert</Badge></div>
                  </td>
                  <td>{r.status}</td>
                  <td className="tabular-nums">{Math.round(r.priority).toLocaleString("tr-TR")}</td>
                  <td className="tabular-nums">{tl(r.total_amount_try)}</td>
                  <td className={r.internal_sla_breached ? "font-semibold text-rose-700 dark:text-rose-400" : ""}>{slaText(r)}</td>
                  <td title={when(r.masak_deadline)}>{r.masak_business_days_left ?? "—"} iş günü</td>
                  <td>{r.assigned_to ?? "—"}</td>
                  <td>{!r.assigned_to && <Button variant="ghost" onClick={() => assign(r.id)} aria-label={`Vaka #${r.id} üstlen`}>Üstlen</Button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {total > 0 && pager}
    </Card>
  );
}
