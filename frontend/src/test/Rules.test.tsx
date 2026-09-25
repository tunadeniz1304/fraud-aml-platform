import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import RulesPage from "../pages/Rules";
import { login, mockFetch } from "./mockFetch";

const RULES = {
  version: "rules-v12",
  fields: { amount_ratio: "Tutar / müşteri ortalaması" },
  rules: [
    {
      id: "R_BIG_AMOUNT", name: "Büyük tutar", when: "amount_ratio >= 10", score: 0.3, description: "", reason_template: "x",
      action_hint: "HOLD", severity: "high", enabled: true, version: 3, tags: [],
    },
    {
      id: "R_NEW_DEVICE", name: "Yeni cihaz", when: "is_new_device", score: 0.2, description: "", reason_template: "y",
      action_hint: null, severity: "medium", enabled: false, version: 1, tags: [],
    },
  ],
};

describe("RulesPage", () => {
  it("lists rules and backtests an edited condition", async () => {
    login("kidemli_analist");
    const fetch = mockFetch((url, init) => {
      if (url === "/api/rules") return { body: RULES };
      if (url === "/api/rules/simulate" && init?.method === "POST")
        return { body: { source: "demo", evaluated: 1000, alerts: 12, alert_rate: 0.012, precision: 0.5, recall: 0.25, true_positives: 6 } };
      return undefined;
    });
    render(<RulesPage />);

    expect(await screen.findByText("R_BIG_AMOUNT")).toBeInTheDocument();
    expect(screen.getByText("Kurallar · rules-v12")).toBeInTheDocument();
    expect(screen.getByText("kapalı")).toBeInTheDocument();

    fireEvent.click(screen.getByText("R_BIG_AMOUNT"));
    const when = screen.getByLabelText("Koşul (when)");
    expect(when).toHaveValue("amount_ratio >= 10");
    fireEvent.change(when, { target: { value: "amount_ratio >= 20" } });
    fireEvent.click(screen.getByRole("button", { name: "Backtest" }));

    await waitFor(() => expect(screen.getByText(/1000 işlemde/)).toBeInTheDocument());
    const call = fetch.mock.calls.find((c) => c[0] === "/api/rules/simulate")!;
    const body = JSON.parse(String(call[1]!.body));
    expect(body.when).toBe("amount_ratio >= 20");
    expect(body).not.toHaveProperty("version");
    expect(screen.getByRole("button", { name: "Kaydet (yeni versiyon)" })).toBeInTheDocument();
  });

  it("hides saving for a junior analyst", async () => {
    login("analist");
    mockFetch((url) => (url === "/api/rules" ? { body: RULES } : undefined));
    render(<RulesPage />);
    fireEvent.click(await screen.findByText("R_NEW_DEVICE"));
    expect(screen.queryByRole("button", { name: "Kaydet (yeni versiyon)" })).not.toBeInTheDocument();
  });
});
