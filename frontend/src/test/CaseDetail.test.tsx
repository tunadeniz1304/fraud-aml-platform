import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import CaseDetailPage from "../pages/CaseDetail";
import { login, mockFetch } from "./mockFetch";

// Cytoscape needs a real canvas; the accessible table alternative is what we assert on.
vi.mock("cytoscape", () => ({ default: vi.fn(() => ({ destroy: vi.fn() })) }));

const CASE = {
  id: 7, customer_id: "C0001", title: "Katır hesap halkası", status: "YENI", case_type: "MULE", ring_id: "RING-1",
  total_amount_try: 125000, assigned_to: null, decision: null, sib_status: null, sib_draft: null, summary: null,
  masak_deadline: "2026-10-05T00:00:00Z", masak_business_days_left: 8,
  alerts: [
    {
      id: 1, transaction_id: "TX-111", alert_type: "MULE", decision: "HOLD", risk_score: 0.91, amount_try: 50000,
      reason_codes: [{ code: "R_FAN_IN", text: "Kısa sürede çok sayıda gönderen", source: "rules", weight: 0.4 }],
      transaction: null,
      scoring: { components: { shap: [{ feature: "amount_ratio", value: 0.3, x: 12 }] }, reason_codes: [], status: "ok" },
    },
  ],
  events: [{ id: 1, event_type: "CREATED", actor: "system", payload: {}, created_at: "2026-09-25T10:00:00Z" }],
  approvals: [{ id: 3, kind: "SIB", status: "BEKLIYOR", requested_by: "kidemli_analist" }],
};
const GRAPH = {
  nodes: [
    { data: { id: "c:C0001", label: "C0001", type: "customer", center: true } },
    { data: { id: "a:A9", label: "A9", type: "account", fraud: true } },
  ],
  edges: [{ data: { id: "e1", source: "c:C0001", target: "a:A9", type: "transfer" } }],
};

describe("CaseDetailPage", () => {
  it("renders the case header, alerts, graph table and the four-eyes approval", async () => {
    login("admin");
    const fetch = mockFetch((url, init) => {
      if (url === "/api/cases/7") return { body: CASE };
      if (url.startsWith("/api/graph/customer/C0001")) return { body: GRAPH };
      if (url === "/api/approvals/3/approve" && init?.method === "POST") return { body: { id: 3, status: "ONAYLANDI" } };
      return undefined;
    });
    render(<CaseDetailPage caseId={7} />);

    expect(await screen.findByRole("heading", { level: 1, name: "#7 Katır hesap halkası" })).toBeInTheDocument();
    expect(screen.getByText("RING-1")).toBeInTheDocument();
    expect(screen.getByText("TX-111", { selector: "span.font-mono" })).toBeInTheDocument();
    expect(screen.getByText("R_FAN_IN")).toBeInTheDocument();
    expect(screen.getByText(/Kısa sürede çok sayıda gönderen/)).toBeInTheDocument();

    const nodes = await screen.findByRole("table", { name: "Düğümler" });
    expect(within(nodes).getByText("doğrulanmış fraud")).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: "Bağlantılar" })).getByText("transfer")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Onay talebi #3 onayla" }));
    await waitFor(() => expect(fetch.mock.calls.some((c) => c[0] === "/api/approvals/3/approve")).toBe(true));
  });
});
