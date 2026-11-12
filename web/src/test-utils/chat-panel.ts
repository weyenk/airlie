// Test helper: a server for the chat panel's routes (threads, pins, the repositories airlie lists), and the
// matchMedia a phone-width window answers with.

import type { ThreadRow } from "@/api/sessions";

export function thread(id: string, over: Partial<ThreadRow> = {}): ThreadRow {
  return { id, kind: "plain", title: "", repo: null, base_ref: null, branch: null, running: false, created: 1, updated: 1, ...over };
}

export type PanelServer = {
  threads: ThreadRow[];
  pins: string[];
  repos: string[];
  /** Routes that answer 500. */
  down: Set<"threads" | "repos">;
  /** The pin routes refuse with this 422 reason. */
  refusePin?: string;
  calls: { method: string; url: string; body: unknown }[];
};

export function panelServer(init: Partial<PanelServer> = {}) {
  const server: PanelServer = { threads: [], pins: [], repos: [], down: new Set(), calls: [], ...init };
  const fetchMock = vi.fn(async (url: string, req?: RequestInit) => {
    const method = req?.method ?? "GET";
    const body = typeof req?.body === "string" ? JSON.parse(req.body) : undefined;
    if (url === "/v1/events") return new Promise<Response>(() => {});
    server.calls.push({ method, url, body });
    if (url === "/v1/airlie/chat/threads" && method === "GET") {
      return server.down.has("threads") ? Response.json({ detail: "threads are down" }, { status: 500 }) : Response.json(server.threads);
    }
    if (url === "/v1/airlie/chat/threads" && method === "POST") {
      const row = thread(`new-${server.threads.length + 1}`, {
        kind: body.kind,
        repo: body.repo ?? null,
        branch: body.kind === "repository" ? "airlie/chat-abc123def456" : null,
        updated: 100,
      });
      server.threads.push(row);
      return Response.json(row, { status: 201 });
    }
    if (url === "/v1/airlie/chat/pins") return Response.json({ pins: server.pins });
    if (url.startsWith("/v1/airlie/chat/pins/")) {
      const repo = url.slice("/v1/airlie/chat/pins/".length);
      if (method === "PUT") {
        if (server.refusePin) return Response.json({ detail: server.refusePin }, { status: 422 });
        if (!server.repos.includes(repo)) return Response.json({ detail: `unknown repository ${repo}` }, { status: 422 });
        server.pins.push(repo);
      } else server.pins = server.pins.filter((p) => p !== repo);
      return new Response(null, { status: 204 });
    }
    if (url === "/v1/airlie/repos/names") {
      return server.down.has("repos") ? Response.json({ detail: "no repositories" }, { status: 500 }) : Response.json({ repos: server.repos });
    }
    return Response.json({ detail: `unexpected ${method} ${url}` }, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return server;
}

/** The window is `width` px wide: matchMedia answers the app's `(max-width: N px)` queries for it. */
export function stubWindowWidth(width: number): void {
  vi.stubGlobal(
    "matchMedia",
    (query: string) => {
      const max = /max-width:\s*(\d+)px/.exec(query);
      return {
        matches: max ? width <= Number(max[1]) : false,
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
        addListener: () => {},
        removeListener: () => {},
      };
    },
  );
}
