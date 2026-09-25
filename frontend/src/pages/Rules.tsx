import { useEffect, useState } from "react";
import { api, canSenior } from "../api";
import { Badge, Button, Card, ErrorNote } from "../components/ui";

type Rule = {
  id: string; name: string; when: string; score: number; description: string; reason_template: string;
  action_hint: string | null; severity: string; enabled: boolean; version: number; tags: string[];
};
type Approval = { id: number; kind: string; target_id: string; status: string; requested_by: string };
type Backtest = { source: string; evaluated: number; alerts: number; alert_rate: number; precision: number | null; recall: number | null; true_positives: number };

export default function RulesPage() {
  const [rules, setRules] = useState<Rule[]>([]);
  const [version, setVersion] = useState("");
  const [fields, setFields] = useState<Record<string, string>>({});
  const [edit, setEdit] = useState<Rule | null>(null);
  const [result, setResult] = useState<Backtest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pending, setPending] = useState<Approval[]>([]);

  const load = () =>
    Promise.all([
      api<{ version: string; rules: Rule[]; fields: Record<string, string> }>("/api/rules").then((r) => {
        setRules(r.rules);
        setVersion(r.version);
        setFields(r.fields);
      }),
      api<Approval[]>("/api/approvals?status=BEKLIYOR")
        .then((a) => setPending((a ?? []).filter((x) => x.kind === "RULE_CHANGE")))
        .catch(() => setPending([])),
    ]);
  useEffect(() => {
    load().catch((e) => setError(e.message));
  }, []);

  const simulate = async () => {
    if (!edit) return;
    setError(null);
    try {
      const { version: _v, enabled: _e, ...draft } = edit;
      setResult(await api<Backtest>("/api/rules/simulate", { method: "POST", json: { ...draft, action_hint: draft.action_hint || null } }));
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const save = async () => {
    if (!edit) return;
    setError(null);
    try {
      const { version: _v, ...body } = edit;
      const exists = rules.some((r) => r.id === edit.id);
      const res = await api<{ approval?: Approval }>(exists ? `/api/rules/${edit.id}` : "/api/rules", { method: exists ? "PUT" : "POST", json: { ...body, action_hint: body.action_hint || null } });
      // maker-checker: the change is live only after a second user approves it
      setNotice(res?.approval ? `Onay bekliyor (#${res.approval.id}) — değişiklik ikinci bir kıdemli kullanıcı onaylayınca devreye girer.` : null);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const decide = async (id: number, verb: "approve" | "reject") => {
    setError(null);
    try {
      await api(`/api/approvals/${id}/${verb}`, { method: "POST", json: {} });
      setNotice(null);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_1.1fr]">
      <Card title={`Kurallar · ${version}`} actions={canSenior() && <Button variant="ghost" onClick={() => { setResult(null); setEdit({ id: "R_YENI_KURAL", name: "Yeni kural", when: "amount_ratio >= 10", score: 0.3, description: "", reason_template: "Tutar ortalamanın {amount_ratio:.1f} katı", action_hint: null, severity: "medium", enabled: true, version: 0, tags: [] }); }}>+ Yeni</Button>}>
        <ul className="max-h-[70vh] space-y-1 overflow-y-auto">
          {rules.map((r) => (
            <li key={r.id}>
              <button className="w-full rounded-lg px-2 py-1 text-left hover:bg-slate-100 dark:hover:bg-slate-800" onClick={() => { setEdit(r); setResult(null); }}>
                <div className="flex items-center gap-2 text-sm">
                  <span className="font-mono">{r.id}</span>
                  {!r.enabled && <Badge tone="rose">kapalı</Badge>}
                  {r.action_hint && <Badge tone="amber">{r.action_hint}</Badge>}
                  <span className="ml-auto text-xs text-slate-600 dark:text-slate-400">v{r.version} · {r.score}</span>
                </div>
                <code className="block truncate text-xs text-slate-600 dark:text-slate-400">{r.when}</code>
              </button>
            </li>
          ))}
        </ul>
        {pending.length > 0 && (
          <div className="mt-3 border-t pt-2 text-sm">
            <div className="mb-1 font-semibold">Onay bekleyen kural değişiklikleri</div>
            {pending.map((a) => (
              <div key={a.id} className="flex items-center gap-2">
                <span className="font-mono">#{a.id} {a.target_id}</span>
                <span className="text-xs text-slate-600 dark:text-slate-400">talep: {a.requested_by}</span>
                {canSenior() && (
                  <span className="ml-auto flex gap-1">
                    <Button variant="ghost" onClick={() => decide(a.id, "approve")}>Onayla</Button>
                    <Button variant="ghost" onClick={() => decide(a.id, "reject")}>Reddet</Button>
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>
      <Card title="Kural düzenleyici (güvenli DSL — eval yok)">
        {!edit ? <p className="text-sm text-slate-600 dark:text-slate-400">Soldan bir kural seçin.</p> : (
          <div className="space-y-2 text-sm">
            <label className="block">Kimlik <input className="w-full font-mono" value={edit.id} onChange={(e) => setEdit({ ...edit, id: e.target.value })} disabled={rules.some((r) => r.id === edit.id && r.version > 0)} /></label>
            <label className="block">Ad <input className="w-full" value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} /></label>
            <label className="block">Koşul (when)
              <textarea className="h-20 w-full font-mono" value={edit.when} onChange={(e) => setEdit({ ...edit, when: e.target.value })} />
            </label>
            <label className="block">Neden şablonu <input className="w-full" value={edit.reason_template} onChange={(e) => setEdit({ ...edit, reason_template: e.target.value })} /></label>
            <div className="flex flex-wrap gap-3">
              <label>Skor <input type="number" step="0.05" min="0.05" max="1" value={edit.score} onChange={(e) => setEdit({ ...edit, score: Number(e.target.value) })} /></label>
              <label>Aksiyon tabanı <select value={edit.action_hint ?? ""} onChange={(e) => setEdit({ ...edit, action_hint: e.target.value || null })}><option value="">—</option>{["STEP_UP", "HOLD", "BLOCK"].map((a) => <option key={a}>{a}</option>)}</select></label>
              <label>Önem <select value={edit.severity} onChange={(e) => setEdit({ ...edit, severity: e.target.value })}>{["low", "medium", "high", "critical"].map((a) => <option key={a}>{a}</option>)}</select></label>
              <label className="flex items-center gap-1"><input type="checkbox" checked={edit.enabled} onChange={(e) => setEdit({ ...edit, enabled: e.target.checked })} /> etkin</label>
            </div>
            <ErrorNote error={error} />
            {notice && <p className="rounded-lg bg-amber-50 p-2 text-amber-900 dark:bg-amber-900/30 dark:text-amber-100">{notice}</p>}
            <div className="flex gap-2">
              <Button variant="ghost" onClick={simulate}>Backtest</Button>
              {canSenior() && <Button onClick={save}>Kaydet (yeni versiyon)</Button>}
            </div>
            {result && (
              <div className="rounded-lg bg-slate-50 p-3 dark:bg-slate-800">
                Kaynak <b>{result.source}</b>: {result.evaluated} işlemde <b>{result.alerts}</b> alert (%{(result.alert_rate * 100).toFixed(2)}),
                kesinlik <b>{result.precision == null ? "—" : result.precision.toFixed(3)}</b>, duyarlılık <b>{result.recall == null ? "—" : result.recall.toFixed(3)}</b>
              </div>
            )}
            <details className="text-xs text-slate-600 dark:text-slate-400"><summary>Kullanılabilir alanlar ({Object.keys(fields).length})</summary>
              <ul className="mt-1 grid grid-cols-1 gap-x-4 md:grid-cols-2">{Object.entries(fields).map(([k, v]) => <li key={k}><code>{k}</code> — {v}</li>)}</ul>
            </details>
          </div>
        )}
      </Card>
    </div>
  );
}
