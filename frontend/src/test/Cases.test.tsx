import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import CasesPage, { PAGE_SIZE } from "../pages/Cases";
import { calledUrls, login, mockFetch } from "./mockFetch";

const row = (id: number) => ({
  id, customer_id: `C${id}`, case_type: "ATO", title: `Hesap ele geçirme ${id}`, status: "YENI", priority: 1000 - id,
  assigned_to: null, ring_id: null, alert_count: 2, total_amount_try: 5000, internal_sla_due: null,
  internal_sla_breached: false, sla_remaining_s: 3600, masak_deadline: null, masak_business_days_left: 10, sib_status: null,
});

describe("CasesPage", () => {
  it("renders the server page and drives pager + filters through /api/cases/page", async () => {
    login("analist");
    const fetch = mockFetch((url) => {
      if (!url.startsWith("/api/cases/page")) return undefined;
      const offset = Number(new URL(url, "http://x").searchParams.get("offset"));
      return { body: { items: [row(offset + 1), row(offset + 2)], total: 60, limit: PAGE_SIZE, offset } };
    });
    render(<CasesPage />);

    expect(await screen.findByRole("link", { name: "Hesap ele geçirme 1" })).toHaveAttribute("href", "#/cases/1");
    expect(screen.getByText("1–2 / 60")).toBeInTheDocument();
    const first = new URL(calledUrls(fetch)[0], "http://x");
    expect(first.pathname).toBe("/api/cases/page");
    expect(first.searchParams.get("status")).toBe("OPEN");
    expect(first.searchParams.get("order")).toBe("priority");
    expect(first.searchParams.get("limit")).toBe(String(PAGE_SIZE));
    expect(first.searchParams.get("offset")).toBe("0");

    fireEvent.click(screen.getByRole("button", { name: "Sonraki sayfa" }));
    expect(await screen.findByRole("link", { name: "Hesap ele geçirme 26" })).toBeInTheDocument();
    expect(calledUrls(fetch).some((u) => u.includes("offset=25"))).toBe(true);
    expect(screen.getByText("26–27 / 60")).toBeInTheDocument();

    // a filter change resets to the first page and sends the filter server-side
    fireEvent.change(screen.getByLabelText("Vaka türü"), { target: { value: "MULE" } });
    fireEvent.change(screen.getByLabelText("Durum"), { target: { value: "YENI" } });
    fireEvent.change(screen.getByLabelText("Sıralama"), { target: { value: "sla" } });
    await waitFor(() => {
      const last = new URL(calledUrls(fetch).at(-1)!, "http://x");
      expect(last.searchParams.get("case_type")).toBe("MULE");
      expect(last.searchParams.get("status")).toBe("YENI");
      expect(last.searchParams.get("order")).toBe("sla");
      expect(last.searchParams.get("offset")).toBe("0");
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Önceki sayfa" })).toBeDisabled());
  });

  it("shows the empty state", async () => {
    login("analist");
    mockFetch(() => ({ body: { items: [], total: 0, limit: PAGE_SIZE, offset: 0 } }));
    render(<CasesPage />);
    expect(await screen.findByText("Kuyruk boş.")).toBeInTheDocument();
  });
});
