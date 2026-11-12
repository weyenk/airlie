// Test helper: a server for the chat panel's routes (threads, pins, repositories from GitHub and extras), and the
// matchMedia a phone-width window answers with.

import type { SourceRepo } from "@/api/sources";
import type { ThreadRow } from "@/api/sessions";

export function thread(id: string, over: Partial<ThreadRow> = {}): ThreadRow {
  return { id, kind: "plain", title: "", repo: null, base_ref: null, branch: null, running: false, created: 1, updated: 1, ...over };
}

export type PanelServer = {
  threads: ThreadRow[];
  pins: string[];
  /** Local checkout names a person may pin. */
  repos: string[];
  linked: boolean;
  login: string | null;
  githubRepos: SourceRepo[];
  sourceError: string | null;
  /** Routes that answer 500. */
  down: Set<"threads" | "repos" | "repositories">;
  /** Chats whose delete answers 409 (a turn is running). */
  running: Set<string>;
  /** The pin routes refuse with this 422 reason. */
  refusePin?: string;
  calls: { method: string; url: string; body: unknown }[];
  link: {
    user_code: string;
    verification_uri: string;
    interval: number;
    pollsBeforeLinked: number;
    pollCount: number;
    link_id: string;
    decline: boolean;
    expire: boolean;
    gone: boolean;
    slowAfter: number | null;
    slowInterval: number;
  };
};

function personRepos(server: PanelServer) {
  return {
    sources: [
      {
        name: "github_app",
        title: "GitHub",
        linked: server.linked,
        login: server.login,
        repositories: server.githubRepos,
        error: server.sourceError,
        listed_at: server.linked ? 1 : null,
      },
    ],
    extras: [...server.pins],
    extra_candidates: [...server.repos],
  };
}

export function panelServer(init: Partial<Omit<PanelServer, "link">> & { link?: Partial<PanelServer["link"]> } = {}) {
  const { link: linkInit, ...rest } = init;
  const server: PanelServer = {
    threads: [],
    pins: [],
    repos: [],
    linked: false,
    login: null,
    githubRepos: [],
    sourceError: null,
    down: new Set(),
    running: new Set(),
    calls: [],
    link: {
      user_code: "WXYZ-0000",
      verification_uri: "https://github.com/login/device",
      interval: 5,
      pollsBeforeLinked: 1,
      pollCount: 0,
      link_id: "link-1",
      decline: false,
      expire: false,
      gone: false,
      slowAfter: null,
      slowInterval: 8,
    },
    ...rest,
  };
  if (linkInit) server.link = { ...server.link, ...linkInit };

  const fetchMock = vi.fn(async (url: string, req?: RequestInit) => {
    const method = req?.method ?? "GET";
    const body = typeof req?.body === "string" ? JSON.parse(req.body) : undefined;
    if (url === "/v1/events") return new Promise<Response>(() => {});
    server.calls.push({ method, url, body });
    if (url.startsWith("/v1/airlie/chat/repositories")) {
      if (server.down.has("repositories")) return Response.json({ detail: "repositories are down" }, { status: 500 });
      return Response.json(personRepos(server));
    }
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
    if (url.startsWith("/v1/airlie/chat/threads/") && method === "DELETE") {
      const id = decodeURIComponent(url.slice("/v1/airlie/chat/threads/".length));
      if (server.running.has(id)) return Response.json({ detail: "a turn is running; stop it first" }, { status: 409 });
      server.threads = server.threads.filter((t) => t.id !== id);
      return new Response(null, { status: 204 });
    }
    if (url === "/v1/airlie/chat/pins") return Response.json({ pins: server.pins });
    if (url.startsWith("/v1/airlie/chat/pins/")) {
      const repo = decodeURIComponent(url.slice("/v1/airlie/chat/pins/".length));
      if (method === "PUT") {
        if (server.refusePin) return Response.json({ detail: server.refusePin }, { status: 422 });
        if (!server.repos.includes(repo)) return Response.json({ detail: `unknown repository ${repo}` }, { status: 422 });
        if (!server.pins.includes(repo)) server.pins.push(repo);
      } else server.pins = server.pins.filter((p) => p !== repo);
      return new Response(null, { status: 204 });
    }
    if (url === "/v1/airlie/chat/sources/github_app/link" && method === "POST") {
      server.link.pollCount = 0;
      return Response.json(
        {
          link_id: server.link.link_id,
          user_code: server.link.user_code,
          verification_uri: server.link.verification_uri,
          expires_at: 999,
          interval: server.link.interval,
        },
        { status: 201 },
      );
    }
    if (url.startsWith("/v1/airlie/chat/sources/github_app/link/") && method === "POST") {
      if (server.link.gone) return Response.json({ error: "there is no such link in progress: start again" }, { status: 404 });
      if (server.link.expire) return Response.json({ error: "the code expired: start again" }, { status: 410 });
      if (server.link.decline) return Response.json({ error: "you declined on GitHub" }, { status: 403 });
      server.link.pollCount += 1;
      if (server.link.pollCount > server.link.pollsBeforeLinked) {
        server.linked = true;
        server.login = "weyenk";
        if (!server.githubRepos.length) {
          server.githubRepos = [{ repo: "weyenk/airlie", private: true, push: true, url: "https://github.com/weyenk/airlie" }];
        }
        return Response.json({ state: "linked", login: "weyenk" });
      }
      const interval =
        server.link.slowAfter !== null && server.link.pollCount >= server.link.slowAfter ? server.link.slowInterval : server.link.interval;
      return Response.json({ state: "pending", interval }, { status: 202 });
    }
    if (url === "/v1/airlie/chat/sources/github_app/link" && method === "DELETE") {
      server.linked = false;
      server.login = null;
      server.githubRepos = [];
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

let currentWidth = 1280;
const resizeListeners = new Map<(e: { matches: boolean }) => void, () => boolean>();

/** The window is `px` wide: matchMedia answers the app's `(max-width: N px)` queries for it, and whatever
 * listens to a query hears the change (a resize). */
export function stubWindowWidth(px: number): void {
  currentWidth = px;
  vi.stubGlobal("matchMedia", (query: string) => {
    const max = /max-width:\s*(\d+)px/.exec(query);
    return {
      get matches() {
        return max ? currentWidth <= Number(max[1]) : false;
      },
      media: query,
      addEventListener: (_: string, fn: (e: { matches: boolean }) => void) => resizeListeners.set(fn, () => (max ? currentWidth <= Number(max[1]) : false)),
      removeEventListener: (_: string, fn: (e: { matches: boolean }) => void) => resizeListeners.delete(fn),
    };
  });
  for (const [fn, matches] of resizeListeners) fn({ matches: matches() });
}
