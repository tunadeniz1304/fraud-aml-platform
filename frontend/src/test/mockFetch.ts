import { vi } from "vitest";

export type Route = (url: string, init?: RequestInit) => { status?: number; body: unknown } | undefined;

/** Stubs global fetch; `route` returns the JSON body for a URL (undefined → 404). */
export function mockFetch(route: Route) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const hit = route(url, init);
    const status = hit?.status ?? (hit ? 200 : 404);
    const body = hit ? hit.body : { detail: "not found" };
    return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

export function login(role: "analist" | "kidemli_analist" | "admin") {
  sessionStorage.setItem("anil3.token", "test-token");
  sessionStorage.setItem("anil3.role", role);
}

export const calledUrls = (fn: ReturnType<typeof mockFetch>) => fn.mock.calls.map((c) => String(c[0]));
