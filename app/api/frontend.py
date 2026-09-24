# ruff: noqa: E501  (inline HTML/JS şablonu; F1'de statik dosyalara taşınacak)
import os

API = os.getenv("API_BASE", "")


def render() -> str:
    return """<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<title>Fraud Ajan Dashboard</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--txt:#e2e8f0;--muted:#94a3b8;--ok:#10b981;--warn:#f59e0b;--bad:#ef4444;}
body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--txt);margin:0;padding:24px;}
h1{font-size:22px;} .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;}
.card{background:var(--panel);border-radius:12px;padding:16px;}
.card .num{font-size:30px;font-weight:700;} .card .lbl{color:var(--muted);font-size:13px;}
.ok{color:var(--ok);} .warn{color:var(--warn);} .bad{color:var(--bad);}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px;}
th,td{text-align:left;padding:8px;border-bottom:1px solid #334155;}
th{color:var(--muted);}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;}
.p-BLOKE{background:#7f1d1d;color:#fca5a5;} .p-AKTIF{background:#064e3b;color:#6ee7b7;}
.p-INCELENIYOR{background:#78350f;color:#fcd34d;} .p-GECTI{background:#1e3a8a;color:#93c5fd;}
</style>
</head>
<body>
<h1>🛡 Siber Güvenlik ve Fraud Ajanı — Canlı Dashboard</h1>
<div class="grid" id="statcards"></div>
<h2>Hesap Durumları</h2>
<table><tbody id="accounts"></tbody></table>
<h2>İşlemler</h2>
<table><tr><th>ID</th><th>Müşteri</th><th>Tutar</th><th>Risk</th><th>Mule</th><th>YüksekRiski Ülke</th><th>Açıklama</th></tr><tbody id="txs"></tbody></table>
<h2>Denetim Logu <a href="/api/admin/audit/export" target="_blank" style="font-size:13px;margin-left:8px;">(CSV indir)</a></h2>
<table><tbody id="audit"></tbody></table>
<h2>Risk Dağılımı (Drift)</h2>
<table><tbody id="stats"></tbody></table>
<h2>Son Blokeler</h2>
<table><tbody id="blocks"></tbody></table>
<script>
const api = "` + API + r`";
async function j(u){const r=await fetch(api+u);if(!r.ok)throw new Error(r.status);return r.json();}
function esc(s){return (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function refresh(){
  try{
    const [s,acc,aud,blk,st,tx]=await Promise.all([j('/api/status'),j('/api/admin/accounts'),j('/api/admin/audit?limit=25'),j('/api/blocks'),j('/api/stats'),j('/api/transactions')]);
    document.getElementById('statcards').innerHTML=[
      ['İzlenen İşlem',s.total_monitored,''],['Analiz',s.analyzed,''],
      ['Bloke',s.blocked,'bad'],['Akışta',s.passed,'ok'],['LLM',s.llm_provider,'warn']]
      .map(([l,n,c])=>`<div class="card"><div class="num ${c}">${n}</div><div class="lbl">${esc(l)}</div></div>`).join('');
    document.getElementById('accounts').innerHTML=acc.map(a=>
      `<tr><td>${esc(a.customer_id)}</td><td>${esc(a.name)}</td><td><span class="pill p-${esc(a.hesap_durumu)}">${esc(a.hesap_durumu)}</span></td></tr>`).join('');
    document.getElementById('txs').innerHTML=tx.slice(-15).reverse().map(t=>
      `<tr><td>${esc(t.transaction_id)}</td><td>${esc(t.customer_id)}</td><td>${t.amount.toFixed(0)} ${esc(t.currency)}</td>
       <td class="${t.risk_score>=0.75?'bad':t.risk_score>=0.5?'warn':'ok'}">${t.risk_score.toFixed(2)}</td>
       <td>${(t.mule_score||0)>0?'<span class="pill p-BLOKE">mule</span>':'-'}</td>
       <td>${t.high_risk_country?'<span class="pill p-BLOKE">evet</span>':'hayır'}</td>
       <td>${esc((t.risk_explanation||[]).join(', ')||'-')}</td></tr>`).join('');
    document.getElementById('audit').innerHTML=aud.map(r=>
      `<tr><td>${esc(r.created_at)}</td><td>${esc(r.transaction_id)}</td><td>${esc(r.customer_id)}</td>
       <td>${r.risk_score.toFixed(2)}</td><td><span class="pill p-${esc(r.decision)}">${esc(r.decision)}</span></td></tr>`).join('');
    document.getElementById('blocks').innerHTML=blk.map(b=>
      `<tr><td>${esc(b.transaction_id)}</td><td>${esc(b.customer_id)}</td><td class="bad">${b.risk_score.toFixed(2)}</td>
       <td>${esc(b.reason)}</td></tr>`).join('');
    document.getElementById('stats').innerHTML=Object.entries(st.histogram||{}).slice(0,12).map(([k,v])=>
      `<tr><td>≈${esc(k)}</td><td>${v}</td></tr>`).join('')+
      `<tr><td>n</td><td>${st.n}</td></tr><tr><td>ort</td><td>${st.mean}</td></tr><tr><td>std</td><td>${st.stdev}</td></tr>`;
  }catch(e){console.error(e);}
}
refresh();setInterval(refresh,2000);
</script>
</body>
</html>"""
