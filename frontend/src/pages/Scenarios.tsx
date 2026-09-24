import { useEffect, useState } from "react";
import { api } from "../api";
import { Badge, Button, Card, DecisionBadge, ErrorNote } from "../components/ui";

type Scenario = { name: string; title: string; expected: string };
type Run = {
  title: string; expected: string; customers: string[];
  results: { transaction_id: string; decision: string; risk_score: number; reasons: string[]; ring_id: string | null; app_warning: string | null }[];
  cases: { id: number; case_type: string; ring_id: string | null; sib_status: string | null }[];
  rings: { id: string; stats: { summary: string } }[];
};

export default function ScenariosPage() {
  const [list, setList] = useState<Scenario[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api<Scenario[]>("/api/scenarios").then(setList).catch((e) => setError(e.message));
  }, []);
  const trigger = async (name: string) => {
    setBusy(name);
    setError(null);
    try {
      setRun(await api<Run>(`/api/scenarios/${name}`, { method: "POST" }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="space-y-4">
      <Card title="Senaryo tetikleyici — sistemin tepkisini canlı izleyin">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
          {list.map((s) => (
            <Button key={s.name} variant="ghost" className="h-20 text-left" onClick={() => trigger(s.name)} disabled={!!busy}>
              <div className="font-semibold">{s.title}</div>
              <div className="text-xs text-slate-500">beklenen: {s.expected}</div>
            </Button>
          ))}
        </div>
        {busy && <p className="mt-2 text-sm text-slate-500">“{busy}” enjekte ediliyor…</p>}
        <ErrorNote error={error} />
      </Card>
      {run && (
        <Card title={`${run.title} · müşteriler: ${run.customers.join(", ")}`}>
          <ul className="space-y-2">
            {run.results.map((r) => (
              <li key={r.transaction_id} className="rounded-lg border border-slate-200 p-2 text-sm dark:border-slate-800">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs">{r.transaction_id}</span>
                  <DecisionBadge decision={r.decision} />
                  <span className="text-xs">risk {r.risk_score?.toFixed(2)} · beklenen {run.expected}</span>
                  {r.ring_id && <Badge tone="rose">{r.ring_id}</Badge>}
                </div>
                <ul className="mt-1 list-disc pl-5 text-xs text-slate-600 dark:text-slate-300">{r.reasons.map((x) => <li key={x}>{x}</li>)}</ul>
                {r.app_warning && <p className="mt-1 rounded bg-amber-50 p-2 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-200">Müşteriye gösterilen uyarı: {r.app_warning}</p>}
              </li>
            ))}
          </ul>
          <div className="mt-3 flex flex-wrap gap-2 text-sm">
            {run.cases.map((c) => <a key={c.id} className="text-indigo-600 underline" href={`#/cases/${c.id}`}>Vaka #{c.id} ({c.case_type}{c.sib_status ? `, ${c.sib_status}` : ""})</a>)}
            {run.rings.map((g) => <Badge key={g.id} tone="rose">{g.stats.summary}</Badge>)}
          </div>
        </Card>
      )}
    </div>
  );
}
