"""Dashboard screenshots for the docs (docs/img/*.png).

Needs a running server that serves the built SPA under /app/ (demo users seeded,
LLM_MODE=demo is fine) and Python Playwright with Chromium::

    python -m playwright install chromium
    python scripts/screenshots.py --base-url http://localhost:8010

Writes live.png, case_detail.png, graph.png, validation.png and rules.png
(viewport 1440x900).
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path

from playwright.sync_api import Locator, Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
VIEWPORT = {"width": 1440, "height": 900}
USERS = {
    "kidemli_analist": "kidemli123",
    "admin": "admin123",
}


def _post(base: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as res:  # noqa: S310 - local dev server
        return json.loads(res.read())


def _login(page: Page, username: str) -> None:
    page.goto("/app/#/login")
    page.get_by_label("Kullanıcı adı").fill(username)
    page.get_by_label("Parola").fill(USERS[username])
    page.get_by_role("button", name="Giriş yap").click()
    expect(page.get_by_role("navigation", name="Ana menü")).to_be_visible()


def _shot(target: Page | Locator, out: Path, name: str) -> None:
    path = out / name
    target.screenshot(path=str(path))
    print(f"{path.relative_to(ROOT)}  {path.stat().st_size // 1024} KB")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=os.environ.get("SCREENSHOT_BASE_URL", "http://localhost:8010"))
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "img")
    parser.add_argument("--light", action="store_true", help="light theme instead of dark")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    token = _post(base, "/api/auth/login", body={"username": "kidemli_analist", "password": USERS["kidemli_analist"]})[
        "access_token"
    ]
    mule = _post(base, "/api/scenarios/mule_ring", token)
    mule_case = next((c["id"] for c in mule["cases"] if c["case_type"] == "MULE"), mule["cases"][0]["id"])

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(base_url=base, viewport=VIEWPORT, device_scale_factor=1)
        theme = "light" if args.light else "dark"
        context.add_init_script(f"localStorage.setItem('anil3.theme', '{theme}')")
        page = context.new_page()

        # live stream (a scenario keeps the table populated even after a batch replay drained)
        _login(page, "kidemli_analist")
        expect(page.get_by_text("● bağlı (SSE)")).to_be_visible(timeout=20_000)
        _post(base, "/api/scenarios/smurfing", token)
        _post(base, "/api/scenarios/ato", token)
        expect(page.locator("table tbody tr").first).to_be_visible(timeout=20_000)
        page.wait_for_timeout(1500)
        _shot(page, out, "live.png")

        # case detail of the mule ring case
        page.goto(f"/app/#/cases/{mule_case}")
        expect(page.get_by_role("heading", level=1)).to_contain_text(f"#{mule_case}")
        page.get_by_role("button", name="Özetle").click()
        expect(page.get_by_text("mod: demo")).to_be_visible(timeout=30_000)
        page.wait_for_timeout(1500)
        _shot(page, out, "case_detail.png")

        # graph card of the mule ring case
        graph = page.locator("section").filter(has=page.get_by_role("heading", name="Varlık ağı (2 adım)"))
        graph.scroll_into_view_if_needed()
        expect(graph.locator("canvas").first).to_be_visible(timeout=20_000)
        page.wait_for_timeout(1500)
        _shot(graph, out, "graph.png")

        # rules studio with a rule open in the editor
        page.goto("/app/#/rules")
        first_rule = page.locator("ul li button").first
        expect(first_rule).to_be_visible()
        first_rule.click()
        page.get_by_role("button", name="Backtest").click()
        page.wait_for_timeout(2500)
        _shot(page, out, "rules.png")

        # validation page as admin
        page.get_by_role("button", name="Çıkış").click()
        _login(page, "admin")
        page.goto("/app/#/validation")
        expect(page.get_by_role("table", name="PaySim — katman bazında metrikler")).to_be_visible(timeout=20_000)
        page.wait_for_timeout(500)
        _shot(page, out, "validation.png")

        browser.close()


if __name__ == "__main__":
    main()
