import cytoscape from "cytoscape";
import { useEffect, useRef, useState } from "react";
import { api, auth, canSenior, streamUrl, tl, when } from "../api";
import { Badge, Button, Card, DecisionBadge, Empty, ErrorNote, RiskBar } from "../components/ui";

type Reason = { code: string; text: string; source: string; weight: number };
type Alert = {
  id: number;
  transaction_id: string;
  alert_type: string;
  decision: string;
  risk_score: number;
  amount_try: number;
  reason_codes: Reason[];
  transaction: Record<string, unknown> | null;
  scoring: { components: Record<string, unknown>; reason_codes: Reason[]; status: string } | null;
};
type CaseDetail = {
  id: number;
  customer_id: string;
  title: string;
  status: string;
  case_type: string;
  ring_id: string | null;
  total_amount_try: number;
  assigned_to: string | null;
  decision: string | null;
  sib_status: string | null;
  sib_draft: Record<string, unknown> | null;
  summary: { bullets: { text: string; citations: string[] }[]; risk_assessment: string } | null;
  masak_deadline: string;
  masak_business_days_left: number;
  alerts: Alert[];
  events: { id: number; event_type: string; actor: string; payload: Record<string, unknown>; created_at: string }[];
  approvals: { id: number; kind: string; status: string; requested_by: string }[];
};
type Shap = { feature: string; value: number; x: number };

function Waterfall({ items }: { items: Shap[] }) {
  if (!items.length) return <Empty>Bu işlem için model açıklaması yok (ALLOW düşük risk).</Empty>;
  const max = Math.max(...items.map((i) => Math.abs(i.value)), 0.01);
  return (
    <ul className="space-y-1" aria-label="SHAP katkıları">
      {items.map((i) => (
        <li key={i.feature} className="grid grid-cols-[10rem_1fr_4rem] items-center gap-2 text-xs">
          <span className="truncate" title={i.feature}>{i.feature} = {Number(i.x).toFixed(2)}</span>
          <div className="relative h-3 rounded bg-slate-100 dark:bg-slate-800">
            <div
              className={i.value >= 0 ? "absolute left-1/2 h-3 rounded-r bg-rose-500" : "absolute right-1/2 h-3 rounded-l bg-emerald-500"}
              style={{ width: `${(Math.abs(i.value) / max) * 50}%` }}
            />
          </div>
          <span className="tabular-nums">{i.value > 0 ? "+" : ""}{i.value.toFixed(2)}</span>
        </li>
      ))}
    </ul>
  );
}

function Graph({ customerId }: { customerId: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cy: cytoscape.Core | undefined;
    api<{ nodes: cytoscape.ElementDefinition[]; edges: cytoscape.ElementDefinition[] }>(`/api/graph/customer/${customerId}?hops=2`)
      .then((g) => {
        if (!ref.current) return;
        cy = cytoscape({
          container: ref.current,
          elements: [...g.nodes, ...g.edges],
          layout: { name: "cose", animate: false },
          style: [
            { selector: "node", style: { label: "data(label)", "font-size": 8, color: "#94a3b8", "background-color": "#6366f1", width: 14, height: 14 } },
            { selector: "node[type = 'account']", style: { "background-color": "#0ea5e9", shape: "round-rectangle" } },
            { selector: "node[type = 'device']", style: { "background-color": "#f59e0b", shape: "diamond" } },
            { selector: "node[type = 'ip']", style: { "background-color": "#64748b", shape: "triangle" } },
            { selector: "node[?fraud]", style: { "background-color": "#e11d48", width: 20, height: 20 } },
            { selector: "node[?center]", style: { "border-width": 3, "border-color": "#22c55e" } },
            { selector: "edge", style: { width: 1, "line-color": "#cbd5e1", "curve-style": "bezier" } },
            { selector: "edge[type = 'transfer']", style: { "target-arrow-shape": "triangle", "line-color": "#818cf8", "target-arrow-color": "#818cf8", width: 2 } },
          ],
        });
      })
      .catch((e) => setError(e.message));
    return () => cy?.destroy();
  }, [customerId]);
  return (
    <div>
      <ErrorNote error={error} />
      <div ref={ref} className="h-80 w-full rounded-lg border border-slate-200 dark:border-slate-800" role="img" aria-label="Varlık ağı grafiği" />
      <p className="mt-1 text-xs text-slate-500">● müşteri ■ hesap ◆ cihaz ▲ IP · kırmızı: doğrulanmış fraud · yeşil çerçeve: bu müşteri</p>
    </div>
  );
}

function Copilot({ c, reload }: { c: CaseDetail; reload: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [rec, setRec] = useState<Record<string, unknown> | null>(null);
  const [meta, setMeta] = useState<string>("");
  const [question, setQuestion] = useState("Bu hesap neden riskli?");
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState<string | null>(null);
  const run = async (kind: "summary" | "recommendation" | "sib") => {
    setBusy(kind);
    setError(null);
    try {
      const r = await api<Record<string, unknown>>(`/api/cases/${c.id}/copilot/${kind}`, { method: "POST" });
      setMeta(`mod: ${r.llm_mode} · araç adımı: ${(r.trace as unknown[]).length} · ${r.llm_latency_ms} ms`);
      if (kind === "recommendation") setRec(r.recommendation as Record<string, unknown>);
      reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  const ask = () => {
    setAnswer("");
    streamUrl(`/api/cases/${c.id}/chat?q=${encodeURIComponent(question)}`).then((url) => {
      const src = new EventSource(url);
      src.onmessage = (e) => {
        const ev = JSON.parse(e.data);
        if (ev.type === "delta") setAnswer((a) => a + ev.text);
        if (ev.type === "done") src.close();
      };
      src.onerror = () => src.close();
    });
  };
  return (
    <Card title="Copilot (öneri — karar analistindir)">
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => run("summary")} disabled={!!busy}>Özetle</Button>
        <Button variant="ghost" onClick={() => run("recommendation")} disabled={!!busy}>Karar öner</Button>
        <Button variant="ghost" onClick={() => run("sib")} disabled={!!busy}>ŞİB taslağı üret</Button>
      </div>
      {busy && <p className="mt-2 text-xs text-slate-500">Copilot araçlarla kanıt topluyor…</p>}
      {meta && <p className="mt-2 text-xs text-slate-500">{meta}</p>}
      <ErrorNote error={error} />
      {c.summary && (
        <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm">
          {c.summary.bullets.map((b, i) => (
            <li key={i}>{b.text} {b.citations.map((x) => <Badge key={x}>{x}</Badge>)}</li>
          ))}
        </ol>
      )}
      {rec && (
        <div className="mt-3 rounded-lg bg-indigo-50 p-3 text-sm dark:bg-indigo-950/40">
          <b>Öneri: {String(rec.decision)}</b> (güven {Number(rec.confidence).toFixed(2)}) — {String(rec.rationale)}
        </div>
      )}
      <div className="mt-3 flex gap-2">
        <input className="flex-1" value={question} onChange={(e) => setQuestion(e.target.value)} aria-label="Copilot'a soru" />
        <Button variant="ghost" onClick={ask}>Sor</Button>
      </div>
      {answer && <p className="mt-2 whitespace-pre-wrap rounded-lg bg-slate-50 p-2 text-sm dark:bg-slate-800">{answer}</p>}
    </Card>
  );
}

function SibEditor({ c, reload }: { c: CaseDetail; reload: () => void }) {
  const [text, setText] = useState(JSON.stringify(c.sib_draft ?? {}, null, 2));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => setText(JSON.stringify(c.sib_draft ?? {}, null, 2)), [c.sib_draft]);
  const save = async () => {
    try {
      await api(`/api/cases/${c.id}/sib`, { method: "PUT", json: { draft: JSON.parse(text) } });
      reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const submit = async () => {
    try {
      await api(`/api/cases/${c.id}/sib/submit`, { method: "POST" });
      reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <Card title={`MASAK ŞİB taslağı · ${c.sib_status ?? "yok"} · son tarih ${when(c.masak_deadline)} (${c.masak_business_days_left} iş günü)`}>
      <textarea className="h-64 w-full font-mono text-xs" value={text} onChange={(e) => setText(e.target.value)} aria-label="ŞİB taslağı (JSON)" />
      <ErrorNote error={error} />
      <div className="mt-2 flex flex-wrap gap-2">
        <Button variant="ghost" onClick={save}>Kaydet</Button>
        <Button onClick={submit} disabled={c.decision !== "FRAUD"}>Onaya gönder (maker-checker)</Button>
        {c.sib_draft && <a className="text-sm text-indigo-600 underline" href={`/api/cases/${c.id}/sib.pdf`} onClick={(e) => { e.preventDefault(); download(`/api/cases/${c.id}/sib.pdf`, `SIB-${c.id}.pdf`); }}>PDF indir</a>}
      </div>
      <p className="mt-2 text-xs text-amber-600">Tipping-off yasağı: bildirimde bulunulduğu bilgisi müşteriyle paylaşılamaz.</p>
    </Card>
  );
}

async function download(path: string, name: string) {
  const res = await fetch(path, { headers: { Authorization: `Bearer ${auth.token()}` } });
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

export default function CaseDetailPage({ caseId }: { caseId: number }) {
  const [c, setC] = useState<CaseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const load = () => api<CaseDetail>(`/api/cases/${caseId}`).then(setC).catch((e) => setError(e.message));
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId]);
  const act = async (path: string, json?: unknown) => {
    setError(null);
    try {
      await api(`/api/cases/${caseId}/${path}`, { method: "POST", json: json ?? {} });
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  if (!c) return <ErrorNote error={error} />;
  const key = [...c.alerts].sort((a, b) => b.risk_score - a.risk_score)[0];
  const shap = ((key?.scoring?.components?.shap as Shap[]) ?? []).slice(0, 10);
  const closed = c.status.startsWith("KAPANDI") || c.status === "SIB_GONDERILDI";
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <a href="#/cases" className="text-sm text-indigo-600">← Kuyruk</a>
        <h1 className="text-lg font-semibold">#{c.id} {c.title}</h1>
        <Badge tone="indigo">{c.case_type}</Badge>
        <Badge>{c.status}</Badge>
        {c.ring_id && <Badge tone="rose">{c.ring_id}</Badge>}
        <span className="text-sm text-slate-500">toplam {tl(c.total_amount_try)} · atanan {c.assigned_to ?? "—"}</span>
      </div>
      <ErrorNote error={error} />
      <div className="flex flex-wrap gap-2">
        {!c.assigned_to && !closed && <Button variant="ghost" onClick={() => act("assign")}>Üstlen</Button>}
        {!closed && <Button variant="danger" onClick={() => act("decision", { outcome: "FRAUD" })}>Fraud olarak kapat</Button>}
        {!closed && <Button variant="success" onClick={() => act("decision", { outcome: "TEMIZ" })}>Temiz olarak kapat</Button>}
        {closed && canSenior() && <Button variant="ghost" onClick={() => act("status", { status: "INCELENIYOR" })}>Yeniden aç</Button>}
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Alert'ler ve reason code'lar">
          {c.alerts.map((a) => (
            <div key={a.id} className="mb-3 border-b border-slate-100 pb-2 last:border-0 dark:border-slate-800">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="font-mono text-xs">{a.transaction_id}</span>
                <DecisionBadge decision={a.decision} />
                <RiskBar value={a.risk_score} />
                <span>{tl(a.amount_try)}</span>
                <Badge>{a.alert_type}</Badge>
              </div>
              <ul className="mt-1 list-disc pl-5 text-xs text-slate-600 dark:text-slate-300">
                {a.reason_codes.map((r) => <li key={r.code}><b>{r.code}</b>: {r.text}</li>)}
              </ul>
            </div>
          ))}
        </Card>
        <Card title={`Model açıklaması (SHAP) · ${key?.transaction_id ?? ""}`}><Waterfall items={shap} /></Card>
        <Card title="Varlık ağı (2 adım)"><Graph customerId={c.customer_id} /></Card>
        <Copilot c={c} reload={load} />
        <Card title="Zaman çizelgesi">
          <ol className="max-h-80 space-y-1 overflow-y-auto text-xs">
            {c.events.map((e) => (
              <li key={e.id}><span className="text-slate-500">{when(e.created_at)}</span> <b>{e.event_type}</b> · {e.actor} {e.payload?.text ? `— ${String(e.payload.text)}` : ""}</li>
            ))}
          </ol>
          <div className="mt-2 flex gap-2">
            <input className="flex-1" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Not ekle" aria-label="Not" />
            <Button variant="ghost" onClick={() => { if (note.trim()) act("notes", { text: note }).then(() => setNote("")); }}>Ekle</Button>
          </div>
        </Card>
        <SibEditor c={c} reload={load} />
      </div>
    </div>
  );
}
