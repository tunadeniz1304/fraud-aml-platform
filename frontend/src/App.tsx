import { useEffect, useState } from "react";
import { api, auth } from "./api";
import { Button, ErrorNote, cx } from "./components/ui";
import LivePage from "./pages/Live";
import CasesPage from "./pages/Cases";
import CaseDetailPage from "./pages/CaseDetail";
import RulesPage from "./pages/Rules";
import ModelsPage from "./pages/Models";
import ScenariosPage from "./pages/Scenarios";

const NAV = [
  { hash: "#/live", label: "Canlı akış" },
  { hash: "#/cases", label: "Vaka kuyruğu" },
  { hash: "#/rules", label: "Kural stüdyosu" },
  { hash: "#/models", label: "Model izleme" },
  { hash: "#/scenarios", label: "Senaryo (demo)" },
];

function useHash() {
  const [hash, setHash] = useState(window.location.hash || "#/live");
  useEffect(() => {
    const on = () => setHash(window.location.hash || "#/live");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

function useTheme() {
  const [dark, setDark] = useState(() => localStorage.getItem("anil3.theme") !== "light");
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("anil3.theme", dark ? "dark" : "light");
  }, [dark]);
  return [dark, setDark] as const;
}

function Login({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState("analist");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const r = await api<{ access_token: string; role: string }>("/api/auth/login", {
        method: "POST",
        json: { username, password },
      });
      auth.save(r.access_token, r.role);
      onDone();
    } catch (err) {
      setError((err as Error).message);
    }
  };
  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <form onSubmit={submit} className="w-full max-w-sm space-y-3 rounded-2xl border border-slate-200 bg-white p-6 shadow dark:border-slate-800 dark:bg-slate-900">
        <h1 className="text-xl font-semibold">Anil3 · Fraud & AML Konsolu</h1>
        <p className="text-sm text-slate-500">Demo kullanıcılar: analist / analist123 · kidemli_analist / kidemli123 · admin / admin123</p>
        <label className="block text-sm">Kullanıcı adı
          <input className="mt-1 w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
        </label>
        <label className="block text-sm">Parola
          <input type="password" className="mt-1 w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
        </label>
        <ErrorNote error={error} />
        <Button type="submit" className="w-full">Giriş yap</Button>
      </form>
    </main>
  );
}

export default function App() {
  const hash = useHash();
  const [dark, setDark] = useTheme();
  const [, force] = useState(0);
  const [llm, setLlm] = useState<string>("");

  useEffect(() => {
    if (auth.token()) api<{ mode: string; model: string }>("/api/llm/status").then((s) => setLlm(`${s.mode === "live" ? "CANLI" : "DEMO"} · ${s.model}`)).catch(() => undefined);
  }, [hash]);

  if (!auth.token() || hash === "#/login") {
    return <Login onDone={() => { window.location.hash = "#/live"; force((x) => x + 1); }} />;
  }
  const caseMatch = hash.match(/^#\/cases\/(\d+)/);
  let page: JSX.Element;
  if (caseMatch) page = <CaseDetailPage caseId={Number(caseMatch[1])} />;
  else if (hash.startsWith("#/cases")) page = <CasesPage />;
  else if (hash.startsWith("#/rules")) page = <RulesPage />;
  else if (hash.startsWith("#/models")) page = <ModelsPage />;
  else if (hash.startsWith("#/scenarios")) page = <ScenariosPage />;
  else page = <LivePage />;

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-10 border-b border-slate-200 bg-white/90 backdrop-blur dark:border-slate-800 dark:bg-slate-950/90">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-2 px-4 py-2">
          <a href="#/live" className="mr-4 font-bold text-indigo-600 dark:text-indigo-400">Anil3</a>
          <nav className="flex flex-wrap gap-1" aria-label="Ana menü">
            {NAV.map((n) => (
              <a key={n.hash} href={n.hash} className={cx("rounded-lg px-3 py-1.5 text-sm", hash.startsWith(n.hash) ? "bg-indigo-600 text-white" : "hover:bg-slate-100 dark:hover:bg-slate-800")}>
                {n.label}
              </a>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-2 text-xs text-slate-500">
            {llm && <span title="LLM modu">LLM: {llm}</span>}
            <span>{auth.role()}</span>
            <Button variant="ghost" onClick={() => setDark(!dark)} aria-label="Tema değiştir">{dark ? "☀︎" : "☾"}</Button>
            <Button variant="ghost" onClick={() => { auth.clear(); window.location.hash = "#/login"; }}>Çıkış</Button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl p-4">{page}</main>
    </div>
  );
}
