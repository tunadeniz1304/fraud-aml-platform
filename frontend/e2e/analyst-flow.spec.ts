import { expect, test, type Page } from "@playwright/test";

// Full analyst flow against a running server (E2E_BASE_URL, default http://localhost:8010):
// login → live stream → scenario → case → copilot summary → ŞİB draft → maker-checker approval.
// Maker: kidemli_analist requests the ŞİB submission; checker: admin approves it (four-eyes).

const USERS = {
  maker: { username: "kidemli_analist", password: "kidemli123" },
  checker: { username: "admin", password: "admin123" },
};

async function login(page: Page, who: keyof typeof USERS) {
  await page.goto("/app/#/login");
  await page.getByLabel("Kullanıcı adı").fill(USERS[who].username);
  await page.getByLabel("Parola").fill(USERS[who].password);
  await page.getByRole("button", { name: "Giriş yap" }).click();
  await expect(page.getByRole("navigation", { name: "Ana menü" })).toBeVisible();
}

async function bearer(page: Page) {
  return page.evaluate(() => sessionStorage.getItem("anil3.token"));
}

test("analyst flow: live → scenario → case → copilot → ŞİB → four-eyes approval", async ({ page }) => {
  await login(page, "maker");

  // 1) live stream: SSE connects and decisions show up (inject one via the API so a
  //    batch-mode server that already drained its replay still produces events)
  await expect(page.getByRole("link", { name: "Canlı akış" })).toHaveAttribute("aria-current", "page");
  await expect(page.getByText("● bağlı (SSE)")).toBeVisible();
  const token = await bearer(page);
  const warmup = await page.request.post("/api/scenarios/card_testing", { headers: { Authorization: `Bearer ${token}` } });
  expect(warmup.ok()).toBeTruthy();
  await expect(page.locator("table tbody tr").first()).toBeVisible();
  await expect(page.getByText(/işlem gösteriliyor/)).toBeVisible();

  // 2) trigger the ATO scenario from the UI
  await page.getByRole("link", { name: "Senaryo (demo)" }).click();
  await page.getByRole("button", { name: /Hesap ele geçirme \(ATO\)/ }).click();
  const caseLink = page.getByRole("link", { name: /^Vaka #\d+/ }).first();
  await expect(caseLink).toBeVisible();

  // 3) open the resulting case
  await caseLink.click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText("#");
  const caseId = Number((await page.getByRole("heading", { level: 1 }).innerText()).match(/#(\d+)/)?.[1]);
  expect(caseId).toBeGreaterThan(0);
  await expect(page.getByText("Alert'ler ve reason code'lar")).toBeVisible();

  // 4) copilot summary (demo LLM mode is deterministic)
  await page.getByRole("button", { name: "Özetle" }).click();
  await expect(page.getByText(/mod: demo/)).toBeVisible();

  // 5) close as fraud, then generate + save the ŞİB draft
  if (await page.getByRole("button", { name: "Fraud olarak kapat" }).isVisible()) {
    await page.getByRole("button", { name: "Fraud olarak kapat" }).click();
    await expect(page.getByText("KAPANDI_FRAUD")).toBeVisible();
  }
  await page.getByRole("button", { name: "ŞİB taslağı üret" }).click();
  await expect(page.getByLabel("ŞİB taslağı (JSON)")).not.toHaveValue("{}");
  await page.getByRole("button", { name: "Kaydet", exact: true }).click();

  // 6) maker requests the submission (maker-checker)
  await page.getByRole("button", { name: "Onaya gönder (maker-checker)" }).click();
  const approvals = page.getByRole("table", { name: "Bu vakaya ait onay talepleri" });
  await expect(approvals).toContainText("BEKLIYOR");
  await expect(approvals).toContainText("kidemli_analist");

  // the maker cannot approve their own request (four-eyes)
  await approvals.getByRole("button", { name: /onayla$/ }).first().click();
  await expect(page.getByText(/dört göz/).first()).toBeVisible();

  // 7) checker (admin) approves
  await page.getByRole("button", { name: "Çıkış" }).click();
  await login(page, "checker");
  await page.goto(`/app/#/cases/${caseId}`);
  const pending = page.getByRole("table", { name: "Bu vakaya ait onay talepleri" });
  await pending.getByRole("button", { name: /onayla$/ }).first().click();
  await expect(pending).toContainText("ONAYLANDI");
  await expect(page.getByText("SIB_GONDERILDI").first()).toBeVisible();
});
