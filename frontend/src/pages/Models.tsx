import { useEffect, useState } from "react";
import { api, isAdmin } from "../api";
import { Button, Card, ErrorNote, Stat } from "../components/ui";

type Metrics = { pr_auc: number; roc_auc: number; recall_at_1pct_fpr: number; cost_weighted_recall: number } | null;
type Compare = {
  champion: string; challenger: string | null;
  offline: { champion: Metrics; challenger: Metrics };
  online: { shadow_decisions: number; agreement?: number; champion_alert_rate?: number; challenger_alert_rate?: number;
    labelled?: { n: number; champion_pr_auc: number; challenger_pr_auc: number; champion_cost_try: number; challenger_cost_try: number } };
};
type Drift = { observed: number; psi: Record<string, number>; watch: string[]; alerts: string[] };
type Llm = { mode: string; model: string; calls: number; failures: number; last_latency_ms: number | null };

function Row({ label, a, b }: { label: string; a?: number | null; b?: number | null }) {
  const fmt = (v?: number | null) => (v == null ? "—" : v.toFixed(3));
  return <tr><th scope="row" className="normal-case tracking-normal">{label}</th><td className="tabular-nums">{fmt(a)}</td><td className="tabular-nums">{fmt(b)}</td></tr>;
}

export default function ModelsPage() {
  const [cmp, setCmp] = useState<Compare | null>(null);
  const [drift, setDrift] = useState<Drift | null>(null);
  const [llm, setLlm] = useState<Llm | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api<Compare>("/api/models/compare").then(setCmp).catch((e) => setError(e.message));
    api<Drift>("/api/models/drift").then(setDrift).catch(() => undefined);
    api<Llm>("/api/llm/status").then(setLlm).catch(() => undefined);
  }, []);
  const promote = async () => {
    if (!cmp?.challenger) return;
    try {
      const r = await api<{ id: number }>(`/api/models/${cmp.challenger}/promote`, { method: "POST" });
      setMsg(`Terfi talebi #${r.id} oluşturuldu — başka bir kıdemli kullanıcının onayı gerekir.`);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const psi = Object.entries(drift?.psi ?? {});
  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      {msg && <p className="rounded-lg bg-emerald-50 p-2 text-sm text-emerald-700 dark:bg-emerald-950 dark:text-emerald-200">{msg}</p>}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Champion vs challenger" actions={isAdmin() && cmp?.challenger && <Button variant="ghost" onClick={promote}>Challenger'ı terfi et</Button>}>
          <table>
            <caption className="sr-only">Champion ve challenger model metrikleri</caption>
            <thead><tr><th scope="col">Metrik (test)</th><th scope="col">{cmp?.champion}</th><th scope="col">{cmp?.challenger ?? "—"}</th></tr></thead>
            <tbody>
              <Row label="PR-AUC" a={cmp?.offline.champion?.pr_auc} b={cmp?.offline.challenger?.pr_auc} />
              <Row label="ROC-AUC" a={cmp?.offline.champion?.roc_auc} b={cmp?.offline.challenger?.roc_auc} />
              <Row label="Recall @ %1 FPR" a={cmp?.offline.champion?.recall_at_1pct_fpr} b={cmp?.offline.challenger?.recall_at_1pct_fpr} />
              <Row label="Maliyet ağırlıklı recall" a={cmp?.offline.champion?.cost_weighted_recall} b={cmp?.offline.challenger?.cost_weighted_recall} />
              <Row label="Canlı alert oranı (gölge)" a={cmp?.online.champion_alert_rate} b={cmp?.online.challenger_alert_rate} />
              {cmp?.online.labelled && <Row label="Etiketli PR-AUC" a={cmp.online.labelled.champion_pr_auc} b={cmp.online.labelled.challenger_pr_auc} />}
            </tbody>
          </table>
          <p className="mt-2 text-xs text-slate-600 dark:text-slate-400">Gölge skorlama: {cmp?.online.shadow_decisions ?? 0} karar · karar uyumu {cmp?.online.agreement != null ? `%${(cmp.online.agreement * 100).toFixed(1)}` : "—"}. Challenger kararları etkilemez.</p>
        </Card>
        <Card title={`Drift (PSI) · ${drift?.observed ?? 0} gözlem`}>
          {psi.length === 0 ? <p className="text-sm text-slate-600 dark:text-slate-400">Yeterli gözlem yok (en az 50).</p> : (
            <>
            <ul className="space-y-1 text-xs">
              {psi.map(([k, v]) => (
                <li key={k} className="grid grid-cols-[10rem_1fr_3rem] items-center gap-2">
                  <span className="truncate">{k}</span>
                  <div className="h-2 rounded bg-slate-100 dark:bg-slate-800"><div className={v > 0.25 ? "h-2 rounded bg-rose-500" : v > 0.1 ? "h-2 rounded bg-amber-400" : "h-2 rounded bg-emerald-500"} style={{ width: `${Math.min(100, (v / 0.5) * 100)}%` }} /></div>
                  <span className="tabular-nums">{v.toFixed(3)}</span>
                </li>
              ))}
            </ul>
            <details className="mt-2 text-xs">
              <summary className="cursor-pointer">Tablo olarak göster</summary>
              <table>
                <caption className="sr-only">Özellik bazında PSI değerleri</caption>
                <thead><tr><th scope="col">Özellik</th><th scope="col">PSI</th><th scope="col">Durum</th></tr></thead>
                <tbody>{psi.map(([k, v]) => <tr key={k}><th scope="row" className="normal-case tracking-normal">{k}</th><td className="tabular-nums">{v.toFixed(3)}</td><td>{v > 0.25 ? "belirgin drift" : v > 0.1 ? "izle" : "stabil"}</td></tr>)}</tbody>
              </table>
            </details>
            </>
          )}
          {drift?.alerts.length ? <p className="mt-2 text-sm font-semibold text-rose-700 dark:text-rose-400">⚠ Belirgin drift: {drift.alerts.join(", ")}</p> : null}
        </Card>
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="LLM modu" value={llm?.mode === "live" ? "CANLI" : "DEMO"} hint={llm?.model} />
        <Stat label="LLM çağrısı" value={llm?.calls ?? "—"} />
        <Stat label="Fallback / hata" value={llm?.failures ?? "—"} />
        <Stat label="Son LLM gecikmesi" value={llm?.last_latency_ms != null ? `${Math.round(llm.last_latency_ms)} ms` : "—"} />
      </div>
    </div>
  );
}
