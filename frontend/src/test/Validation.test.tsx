import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ValidationPage, { type ValidationData } from "../pages/Validation";
import { login, mockFetch } from "./mockFetch";

const layer = (pr: number) => ({
  pr_auc: pr,
  pr_auc_ci95: [pr - 0.05, pr + 0.05] as [number, number],
  roc_auc: 0.9,
  recall_at_1pct_fpr: 0.5,
  "budget_0.005": { recall: 0.37, precision: 0.574, cost_weighted_recall: 0.7 },
  "budget_0.01": { recall: 0.53, precision: 0.41, cost_weighted_recall: 0.8 },
});
const DATA: ValidationData = {
  paysim: {
    rows: { total: 1000, test: 28124, test_fraud: 219, test_fraud_rate: 0.0078 },
    layers: { rules: layer(0.1059), gbm: layer(0.4661), full: layer(0.4968) },
    ablation: [{ layer: "rules+gbm", delta_pr_auc: 0.3882, ci95: [0.3358, 0.4391], verdict: "katkı var" }],
    actions: { all: { ALLOW: 28034, STEP_UP: 36, HOLD: 29, BLOCK: 25 }, fraud: { ALLOW: 147, STEP_UP: 21, HOLD: 26, BLOCK: 25 } },
  },
  synthetic: { layers: { rules: layer(0.3), gbm: layer(0.55), full: layer(0.56) } },
  elliptic: { results: { all: { illicit_f1: 0.8149, illicit_precision: 0.926, illicit_recall: 0.7276, pr_auc: 0.8036 } } },
  ulb: { rows: { test: 85442 }, test_fraud: 108, results: { gbm: { pr_auc: 0.7969, roc_auc: 0.9826, recall_at_1pct_fpr: 0.8796 } } },
  champion_selection: {
    decision: {
      winner: "fraud_gbm_v3",
      challenger: "fraud_gbm_v4",
      four_eyes: { approval_id: 1, requested_by: "admin", approved_by: "kidemli_analist", result: "ONAYLANDI" },
    },
  },
};

describe("ValidationPage", () => {
  it("renders the PaySim layer table from the API", async () => {
    login("admin");
    mockFetch((url) => (url === "/api/models/validation" ? { body: DATA } : undefined));
    render(<ValidationPage />);

    const table = await screen.findByRole("table", { name: "PaySim — katman bazında metrikler" });
    expect(within(table).getAllByRole("columnheader")).toHaveLength(6);
    const gbm = within(table).getByRole("rowheader", { name: "GBM" }).closest("tr")!;
    expect(gbm).toHaveTextContent("0.466");
    expect(gbm).toHaveTextContent("[0.416, 0.516]");
    expect(gbm).toHaveTextContent("0.370 / 0.574");

    expect(screen.getByRole("table", { name: /Her katmanın PR-AUC katkısı/ })).toHaveTextContent("katkı var");
    expect(screen.getByRole("table", { name: /sentetik veri ile PaySim/ })).toHaveTextContent("0.550");
    const elliptic = screen.getByRole("table", { name: /Elliptic/ });
    expect(elliptic).toHaveTextContent("EvolveGCN");
    expect(elliptic).toHaveTextContent("literatür (Weber vd., 2019)");
    expect(screen.getByText("fraud_gbm_v3")).toBeInTheDocument();
    expect(screen.getByText("kidemli_analist")).toBeInTheDocument();
  });

  it("shows a permission message on 403", async () => {
    login("analist");
    mockFetch(() => ({ status: 403, body: { detail: "Yetersiz yetki" } }));
    render(<ValidationPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("yetkiniz yok");
  });

  it("shows a loading state first", () => {
    login("admin");
    mockFetch(() => ({ body: DATA }));
    render(<ValidationPage />);
    expect(screen.getByRole("status")).toHaveTextContent("yükleniyor");
  });
});
