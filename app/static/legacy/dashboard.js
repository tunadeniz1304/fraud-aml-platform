// Klasik dashboard — CSP uyumlu harici betik (inline JS yok).
// API tabanı sunucu tarafından <meta name="api-base"> içine güvenle yazılır;
// tüm istekler (admin GET'leri dahil) oturum token'ı ile gönderilir.
"use strict";

const API = document.querySelector('meta[name="api-base"]')?.content || "";
const SESSION_KEY = "anil3.token";
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function token() {
  try { return sessionStorage.getItem(SESSION_KEY); } catch { return null; }
}
function setToken(t) {
  try {
    if (t) sessionStorage.setItem(SESSION_KEY, t);
    else sessionStorage.removeItem(SESSION_KEY);
  } catch { /* depolama kapalı olabilir */ }
}

async function api(path, opts = {}) {
  const headers = Object.assign({ Accept: "application/json" }, opts.headers || {});
  const t = token();
  if (t) headers.Authorization = "Bearer " + t;
  const r = await fetch(API + path, Object.assign({}, opts, { headers }));
  if (r.status === 401) { setToken(null); showLogin(); throw new Error("401"); }
  if (!r.ok) throw new Error(String(r.status));
  return r;
}
const j = async (p) => (await api(p)).json();

function esc(s) {
  return (s == null ? "" : String(s)).replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

function showLogin() {
  document.getElementById("login").classList.remove("hidden");
  document.getElementById("app").classList.add("hidden");
  document.getElementById("session").textContent = "";
}

function showApp(me) {
  document.getElementById("login").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
  const s = document.getElementById("session");
  s.innerHTML = `${esc(me.display_name || me.username)} (${esc(me.role)}) ` +
    '<button class="link" id="logout" type="button">çıkış</button>';
  document.getElementById("logout").addEventListener("click", () => { setToken(null); showLogin(); });
}

function card([label, value, cls]) {
  return `<div class="card"><div class="num ${cls}">${esc(value)}</div><div class="lbl">${esc(label)}</div></div>`;
}

async function refresh() {
  if (!token()) return;
  try {
    const me = await j("/api/auth/me");
    const isAdmin = me.role === "admin";
    const [s, aud, blk, st, tx] = await Promise.all([
      j("/api/status"), j("/api/audit?limit=25"), j("/api/blocks"), j("/api/stats"),
      j("/api/transactions?limit=50"),
    ]);
    const acc = isAdmin ? await j("/api/admin/accounts") : s.accounts;
    document.getElementById("statcards").innerHTML = [
      ["İzlenen İşlem", s.total_monitored, ""], ["Reddedilen", s.rejected, "warn"],
      ["Analiz", s.analyzed, ""], ["Bloke", s.blocked, "bad"], ["İncelemede", s.warned, "warn"],
      ["Akışta", s.passed, "ok"], ["LLM", s.llm_mode, "warn"],
    ].map(card).join("");
    document.getElementById("accounts").innerHTML = acc.map((a) =>
      `<tr><td>${esc(a.customer_id)}</td><td>${esc(a.name)}</td>` +
      `<td><span class="pill p-${esc(a.hesap_durumu)}">${esc(a.hesap_durumu)}</span></td></tr>`).join("");
    document.getElementById("txs").innerHTML = tx.slice(-15).reverse().map((t) => {
      const cls = t.risk_score >= 0.75 ? "bad" : t.risk_score >= 0.5 ? "warn" : "ok";
      const mule = (t.mule_score || 0) > 0 ? '<span class="pill p-BLOKE">mule</span>' : "-";
      const hrc = t.high_risk_country ? '<span class="pill p-BLOKE">evet</span>' : "hayır";
      return `<tr><td>${esc(t.transaction_id)}</td><td>${esc(t.customer_id)}</td>` +
        `<td>${Number(t.amount).toFixed(0)} ${esc(t.currency)}</td>` +
        `<td class="${cls}">${Number(t.risk_score).toFixed(2)}</td><td>${mule}</td><td>${hrc}</td>` +
        `<td>${esc((t.risk_explanation || []).join(", ") || "-")}</td></tr>`;
    }).join("");
    document.getElementById("audit").innerHTML = aud.map((r) =>
      `<tr><td>${esc(r.created_at)}</td><td>${esc(r.transaction_id)}</td><td>${esc(r.customer_id)}</td>` +
      `<td>${Number(r.risk_score).toFixed(2)}</td>` +
      `<td><span class="pill p-${esc(r.decision)}">${esc(r.decision)}</span></td></tr>`).join("");
    document.getElementById("blocks").innerHTML = blk.map((b) =>
      `<tr><td>${esc(b.transaction_id)}</td><td>${esc(b.customer_id)}</td>` +
      `<td class="bad">${Number(b.risk_score).toFixed(2)}</td><td>${esc(b.reason)}</td></tr>`).join("");
    document.getElementById("stats").innerHTML = Object.entries(st.histogram || {}).slice(0, 12)
      .map(([k, v]) => `<tr><td>≈${esc(k)}</td><td>${esc(v)}</td></tr>`).join("") +
      `<tr><td>n</td><td>${esc(st.n)}</td></tr><tr><td>ort</td><td>${esc(st.mean)}</td></tr>` +
      `<tr><td>std</td><td>${esc(st.stdev)}</td></tr>`;
    document.getElementById("csv").classList.toggle("hidden", !isAdmin);
    showApp(me);
  } catch (e) {
    console.error(e);
  }
}

async function downloadCsv() {
  const r = await api("/api/admin/audit/export");
  const blob = new Blob([await r.text()], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "audit_log.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const err = document.getElementById("login-error");
    err.textContent = "";
    try {
      const r = await fetch(API + "/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: f.get("username"), password: f.get("password") }),
      });
      if (!r.ok) {
        err.textContent = r.status === 429 ? "Çok fazla deneme, lütfen bekleyin." : "Giriş başarısız.";
        return;
      }
      setToken((await r.json()).access_token);
      refresh();
    } catch {
      err.textContent = "Sunucuya ulaşılamadı.";
    }
  });
  document.getElementById("csv").addEventListener("click", downloadCsv);
  if (token()) refresh();
  else showLogin();
  setInterval(refresh, 3000);
});
