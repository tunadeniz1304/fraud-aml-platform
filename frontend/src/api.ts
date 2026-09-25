// Thin API client: JWT in sessionStorage, JSON helpers, SSE subscriptions.

const STORAGE_JWT = "anil3.token";
const STORAGE_ROLE = "anil3.role";

export type Role = "analist" | "kidemli_analist" | "admin";

export const auth = {
  token: () => sessionStorage.getItem(STORAGE_JWT),
  role: () => (sessionStorage.getItem(STORAGE_ROLE) as Role | null) ?? null,
  save: (token: string, role: string) => {
    sessionStorage.setItem(STORAGE_JWT, token);
    sessionStorage.setItem(STORAGE_ROLE, role);
  },
  clear: () => {
    sessionStorage.removeItem(STORAGE_JWT);
    sessionStorage.removeItem(STORAGE_ROLE);
  },
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T = unknown>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = auth.token();
  if (token) headers.Authorization = `Bearer ${token}`;
  let body = init.body;
  if (init.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(init.json);
  }
  const res = await fetch(path, { ...init, headers: { ...headers, ...(init.headers as object) }, body });
  if (res.status === 401) {
    auth.clear();
    window.location.hash = "#/login";
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  const type = res.headers.get("content-type") ?? "";
  return (type.includes("json") ? res.json() : res.text()) as Promise<T>;
}

/** Single-use, 60 s SSE ticket: EventSource cannot send the bearer header and a
 * JWT in the URL would leak into access logs. */
export async function streamUrl(path: string): Promise<string> {
  const { ticket } = await api<{ ticket: string }>("/api/stream/ticket", { method: "POST" });
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}ticket=${encodeURIComponent(ticket)}`;
}

export function sse(path: string, onEvent: (type: string, data: unknown) => void): () => void {
  let source: EventSource | null = null;
  let closed = false;
  const handler = (type: string) => (e: MessageEvent) => {
    try {
      onEvent(type, JSON.parse(e.data));
    } catch {
      onEvent(type, e.data);
    }
  };
  streamUrl(path)
    .then((url) => {
      if (closed) return;
      source = new EventSource(url);
      source.addEventListener("decision", handler("decision"));
      source.addEventListener("ready", handler("ready"));
      source.onmessage = handler("message");
    })
    .catch(() => onEvent("error", null));
  return () => {
    closed = true;
    source?.close();
  };
}

/** POST a JSON body and read the Server-Sent Events it streams back (fetch
 * sends the bearer header, so no ticket and nothing sensitive in the URL). */
export async function postSse(path: string, json: unknown, onData: (data: unknown) => void): Promise<void> {
  const token = auth.token();
  const res = await fetch(path, {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(json),
  });
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      for (const line of frame.split("\n")) {
        if (!line.startsWith("data:")) continue;
        try {
          onData(JSON.parse(line.slice(5)));
        } catch {
          onData(line.slice(5));
        }
      }
    }
  }
}

export const tl = (v: number | null | undefined) =>
  `${(v ?? 0).toLocaleString("tr-TR", { maximumFractionDigits: 2 })} TL`;

export const when = (v: string | null | undefined) =>
  v ? new Date(v).toLocaleString("tr-TR", { dateStyle: "short", timeStyle: "short" }) : "—";

export const canSenior = () => auth.role() === "kidemli_analist" || auth.role() === "admin";
export const isAdmin = () => auth.role() === "admin";
