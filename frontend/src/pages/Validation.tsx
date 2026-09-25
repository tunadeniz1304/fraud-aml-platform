import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { Badge, Card, Empty, ErrorNote } from "../components/ui";

type Budget = { recall: number; precision: number; cost_weighted_recall: number };
type Layer = {
  pr_auc: number;
  pr_auc_ci95?: [number, number];
  roc_auc: number;
  recall_at_1pct_fpr: number;
  "budget_0.005"?: Budget;
  "budget_0.01"?: Budget;
};
type Ablation = { layer: string; delta_pr_auc: number; ci95: [number, number]; verdict: string };
type Replay = {
  dataset?: string;
  rows?: { total: number; test: number; test_fraud: number; test_fraud_rate: number };
  layers?: Record<string, Layer>;
  ablation?: Ablation[];
  actions?: { all: Record<string, number>; fraud: Record<string, number> };
};
type EllipticRow = { illicit_f1: number; illicit_precision: number; illicit_recall: number; pr_auc: number; features?: number };
type Elliptic = { dataset?: string; nodes?: number; edges?: number; results?: Record<string, EllipticRow> };
type UlbRow = { pr_auc: number; roc_auc: number; recall_at_1pct_fpr: number; budget_1pct?: { precision: number; recall: number; cost_weighted_recall: number } };
type Ulb = { dataset?: string; rows?: { test: number }; test_fraud?: number; results?: Record<string, UlbRow>; limitation?: string };
type Champion = {
  decision?: {
    rule?: string;
    winner: string;
    challenger: string;
    four_eyes?: { approval_id: number; requested_by: string; approved_by: string; self_approval_status?: number; result: string };
  };
};
export type ValidationData = {
  paysim?: Replay;
  paysim_fixture?: Replay;
  synthetic?: Replay;
  elliptic?: Elliptic;
  elliptic_fixture?: Elliptic;
  ulb?: Ulb;
  champion_selection?: Champion;
};

const LAYERS = ["rules", "gbm", "rules+gbm", "+anomaly", "+graph", "full"];
const LAYER_TEXT: Record<string, string> = {
  rules: "Kurallar",
  gbm: "GBM",
  "rules+gbm": "Kurallar + GBM",
  "+anomaly": "+ anomali",
  "+graph": "+ graf",
  full: "Tam sistem",
};
const ACTIONS = ["ALLOW", "STEP_UP", "HOLD", "BLOCK"];

// Weber et al. (2019), "Anti-Money Laundering in Bitcoin", Table 1 — illicit class, temporal split 1-34 / 35-49.
const WEBER: { model: string; f1: number }[] = [
  { model: "Random Forest (AF)", f1: 0.788 },
  { model: "Random Forest (AF + NE)", f1: 0.796 },
  { model: "Logistic Regression (AF)", f1: 0.481 },
  { model: "GCN", f1: 0.628 },
  { model: "Skip-GCN", f1: 0.705 },
  { model: "EvolveGCN", f1: 0.72 },
];

const f3 = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? "—" : v.toFixed(3));
const pct = (v: number | null | undefined) => (v == null ? "—" : `%${(v * 100).toFixed(2)}`);
const ci = (c?: [number, number]) => (c ? `[${f3(c[0])}, ${f3(c[1])}]` : "");
const int = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString("tr-TR"));

function Table({ caption, head, children }: { caption: string; head: string[]; children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table>
        <caption className="mb-2 text-left text-sm font-medium text-slate-700 dark:text-slate-200">{caption}</caption>
        <thead>
          <tr>{head.map((h) => <th key={h} scope="col">{h}</th>)}</tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

function LayerTable({ replay, caption }: { replay: Replay; caption: string }) {
  const layers = LAYERS.filter((l) => replay.layers?.[l]);
  return (
    <Table caption={caption} head={["Katman", "PR-AUC [%95 GA]", "ROC-AUC", "Recall @ %1 FPR", "Recall / kesinlik (%0,5 bütçe)", "Recall / kesinlik (%1 bütçe)"]}>
      {layers.map((l) => {
        const m = replay.layers![l];
        const b05 = m["budget_0.005"];
        const b1 = m["budget_0.01"];
        return (
          <tr key={l}>
            <th scope="row" className="normal-case tracking-normal">{LAYER_TEXT[l] ?? l}</th>
            <td className="tabular-nums">{f3(m.pr_auc)} <span className="text-xs text-slate-600 dark:text-slate-300">{ci(m.pr_auc_ci95)}</span></td>
            <td className="tabular-nums">{f3(m.roc_auc)}</td>
            <td className="tabular-nums">{f3(m.recall_at_1pct_fpr)}</td>
            <td className="tabular-nums">{b05 ? `${f3(b05.recall)} / ${f3(b05.precision)}` : "—"}</td>
            <td className="tabular-nums">{b1 ? `${f3(b1.recall)} / ${f3(b1.precision)}` : "—"}</td>
          </tr>
        );
      })}
    </Table>
  );
}

const VERDICT_TONE: Record<string, "emerald" | "rose" | "slate"> = { "katkı var": "emerald", zarar: "rose" };

export default function ValidationPage() {
  const [data, setData] = useState<ValidationData | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<ValidationData>("/api/models/validation")
      .then(setData)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 403) setForbidden(true);
        else setError((e as Error).message);
      });
  }, []);

  if (forbidden) {
    return (
      <Card title="Doğrulama">
        <p role="alert" className="text-sm">Bu sayfayı görüntüleme yetkiniz yok — doğrulama sonuçları yalnızca kıdemli analist ve yönetici rollerine açıktır.</p>
      </Card>
    );
  }
  if (error) return <ErrorNote error={error} />;
  if (!data) return <p role="status" className="text-sm text-slate-600 dark:text-slate-300">Doğrulama sonuçları yükleniyor…</p>;

  const paysim = data.paysim ?? data.paysim_fixture;
  const paysimIsFixture = !data.paysim && !!data.paysim_fixture;
  const elliptic = data.elliptic ?? data.elliptic_fixture;
  const syn = data.synthetic;
  const champ = data.champion_selection?.decision;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Doğrulama — açık veri setleri</h1>
      {paysim ? (
        <Card title={`PaySim replay${paysimIsFixture ? " (fixture)" : ""}`}>
          <p className="mb-2 text-xs text-slate-600 dark:text-slate-300">
            Test: {int(paysim.rows?.test)} işlem · {int(paysim.rows?.test_fraud)} fraud ({pct(paysim.rows?.test_fraud_rate)}). Kesinlik/recall alarm bütçesine göre (işlemlerin %0,5 ve %1'i).
          </p>
          <LayerTable replay={paysim} caption="PaySim — katman bazında metrikler" />
        </Card>
      ) : <Empty>PaySim sonucu yok.</Empty>}

      <div className="grid gap-4 lg:grid-cols-2">
        {paysim?.ablation && (
          <Card title="Ablasyon (PaySim)">
            <Table caption="Her katmanın PR-AUC katkısı (bootstrap %95 güven aralığı)" head={["Katman", "Δ PR-AUC", "%95 GA", "Sonuç"]}>
              {paysim.ablation.map((a) => (
                <tr key={a.layer}>
                  <th scope="row" className="normal-case tracking-normal">{LAYER_TEXT[a.layer] ?? a.layer}</th>
                  <td className="tabular-nums">{a.delta_pr_auc > 0 ? "+" : ""}{f3(a.delta_pr_auc)}</td>
                  <td className="tabular-nums">{ci(a.ci95)}</td>
                  <td><Badge tone={VERDICT_TONE[a.verdict] ?? "slate"}>{a.verdict}</Badge></td>
                </tr>
              ))}
            </Table>
          </Card>
        )}
        {paysim?.actions && (
          <Card title="Aksiyon dağılımı (PaySim test)">
            <Table caption="Tam sistemin verdiği aksiyonlar" head={["Aksiyon", "Tüm işlemler", "Fraud işlemler"]}>
              {ACTIONS.map((k) => (
                <tr key={k}>
                  <th scope="row" className="normal-case tracking-normal">{k}</th>
                  <td className="tabular-nums">{int(paysim.actions!.all[k] ?? 0)}</td>
                  <td className="tabular-nums">{int(paysim.actions!.fraud[k] ?? 0)}</td>
                </tr>
              ))}
            </Table>
          </Card>
        )}
      </div>

      {syn?.layers && paysim?.layers && (
        <Card title="Sentetik vs gerçek">
          <Table caption="Katman bazında PR-AUC: sentetik veri ile PaySim karşılaştırması" head={["Katman", "Sentetik PR-AUC", "PaySim PR-AUC", "Fark"]}>
            {LAYERS.filter((l) => syn.layers![l] || paysim.layers![l]).map((l) => {
              const a = syn.layers![l]?.pr_auc;
              const b = paysim.layers![l]?.pr_auc;
              return (
                <tr key={l}>
                  <th scope="row" className="normal-case tracking-normal">{LAYER_TEXT[l] ?? l}</th>
                  <td className="tabular-nums">{f3(a)}</td>
                  <td className="tabular-nums">{f3(b)}</td>
                  <td className="tabular-nums">{a != null && b != null ? `${b - a > 0 ? "+" : ""}${f3(b - a)}` : "—"}</td>
                </tr>
              );
            })}
          </Table>
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {elliptic?.results && (
          <Card title={`Elliptic (Bitcoin AML)${!data.elliptic ? " · fixture" : ""}`}>
            <Table caption="Elliptic — illicit sınıfı, zamansal ayrım (adım 1-34 eğitim / 35-49 test)" head={["Özellik seti", "F1", "Kesinlik", "Recall", "PR-AUC"]}>
              {Object.entries(elliptic.results).map(([k, r]) => (
                <tr key={k}>
                  <th scope="row" className="normal-case tracking-normal">{k}</th>
                  <td className="tabular-nums">{f3(r.illicit_f1)}</td>
                  <td className="tabular-nums">{f3(r.illicit_precision)}</td>
                  <td className="tabular-nums">{f3(r.illicit_recall)}</td>
                  <td className="tabular-nums">{f3(r.pr_auc)}</td>
                </tr>
              ))}
              <tr>
                <td colSpan={5} className="bg-slate-50 text-xs text-slate-700 dark:bg-slate-800 dark:text-slate-200">
                  Karşılaştırma için literatür (Weber vd., 2019), Tablo 1 — aynı zamansal ayrım, yalnızca illicit F1:
                </td>
              </tr>
              {WEBER.map((w) => (
                <tr key={w.model}>
                  <th scope="row" className="normal-case tracking-normal">{w.model} <span className="text-xs font-normal">— literatür (Weber vd., 2019)</span></th>
                  <td className="tabular-nums">{f3(w.f1)}</td>
                  <td>—</td>
                  <td>—</td>
                  <td>—</td>
                </tr>
              ))}
            </Table>
          </Card>
        )}
        {data.ulb?.results && (
          <Card title="ULB kredi kartı">
            <Table caption={`ULB — test ${int(data.ulb.rows?.test)} işlem, ${int(data.ulb.test_fraud)} fraud`} head={["Model", "PR-AUC", "ROC-AUC", "Recall @ %1 FPR", "Kesinlik / recall (%1 bütçe)"]}>
              {Object.entries(data.ulb.results).map(([k, r]) => (
                <tr key={k}>
                  <th scope="row" className="normal-case tracking-normal">{k}</th>
                  <td className="tabular-nums">{f3(r.pr_auc)}</td>
                  <td className="tabular-nums">{f3(r.roc_auc)}</td>
                  <td className="tabular-nums">{f3(r.recall_at_1pct_fpr)}</td>
                  <td className="tabular-nums">{r.budget_1pct ? `${f3(r.budget_1pct.precision)} / ${f3(r.budget_1pct.recall)}` : "—"}</td>
                </tr>
              ))}
            </Table>
            {data.ulb.limitation && <p className="mt-2 text-xs text-slate-600 dark:text-slate-300">Sınırlama: {data.ulb.limitation}</p>}
          </Card>
        )}
      </div>

      {champ && (
        <Card title="Champion seçimi">
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="font-medium">Kazanan (champion)</dt><dd className="font-mono">{champ.winner}</dd>
            <dt className="font-medium">Challenger</dt><dd className="font-mono">{champ.challenger}</dd>
            {champ.rule && <><dt className="font-medium">Seçim kuralı</dt><dd>{champ.rule}</dd></>}
            {champ.four_eyes && (
              <>
                <dt className="font-medium">Dört göz onayı</dt>
                <dd>
                  talep: {champ.four_eyes.requested_by} · onaylayan: <b>{champ.four_eyes.approved_by}</b> · sonuç: {champ.four_eyes.result}
                  {champ.four_eyes.self_approval_status ? ` (kendi kendine onay denemesi: HTTP ${champ.four_eyes.self_approval_status})` : ""}
                </dd>
              </>
            )}
          </dl>
        </Card>
      )}
    </div>
  );
}
