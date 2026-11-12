// Test helper: the server the whole chat screen talks to (sign-in, the chat list, a chat's saved thread and status, pins,
// the repositories, the turn route, the attachment limits and commands), in one fetch stub. Rebuilt per test.

import type { SessionStatusData, StoredMessage, ThreadRow } from "@/api/sessions";
import { thread } from "./chat-panel";
import { STATUS } from "./chat-status";

export type ScreenServer = {
  threads: ThreadRow[];
  pins: string[];
  repos: string[];
  /** A chat's saved messages, by chat id. */
  messages: Record<string, StoredMessage[]>;
  status: SessionStatusData;
  /** What `POST /v1/airlie/chat/turn` answers with, given its JSON body. */
  turn: (body: Record<string, unknown>) => Response | Promise<Response>;
  calls: { method: string; url: string; body: unknown }[];
};

const LIMITS = { max_per_message: 10, max_image_mb: 20, max_video_mb: 100, max_file_mb: 25 };
const SKILLS = [
  { name: "story", summary: "Turn a request into a story", action: "story" },
  { name: "clear", summary: "Start a fresh session", action: "clear" },
];

export function screenServer(init: Partial<ScreenServer> = {}): ScreenServer {
  const server: ScreenServer = {
    threads: [],
    pins: [],
    repos: [],
    messages: {},
    status: STATUS,
    turn: () => new Response(null, { status: 500 }),
    calls: [],
    ...init,
  };
  const prefix = "/v1/airlie/chat/threads";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, req?: RequestInit) => {
      const method = req?.method ?? "GET";
      const raw = typeof req?.body === "string" ? JSON.parse(req.body) : undefined;
      if (url === "/v1/events") return new Promise<Response>(() => {});
      server.calls.push({ method, url, body: raw });
      if (url === "/v1/auth/me") return Response.json({ name: "Noah Kruswicki", role: "admin", via: "cookie" });
      if (url === "/v1/airlie/chat/skills") return Response.json({ skills: SKILLS });
      if (url === "/v1/airlie/chat/attachments") return Response.json(LIMITS);
      if (url === "/v1/airlie/engines") return Response.json([]);
      if (url === "/v1/airlie/chat/turn") return server.turn(raw);
      if (url === prefix && method === "GET") return Response.json(server.threads);
      if (url === prefix && method === "POST") {
        const row = thread(`new-${server.threads.length + 1}`, {
          kind: raw.kind,
          repo: raw.repo ?? null,
          branch: raw.kind === "repository" ? "airlie/chat-new" : null,
          updated: 100,
        });
        server.threads.push(row);
        return Response.json(row, { status: 201 });
      }
      if (url === "/v1/airlie/chat/pins") return Response.json({ pins: server.pins });
      if (url === "/v1/airlie/repos/names") return Response.json({ repos: server.repos });
      if (url === "/v1/airlie/stories" && method === "POST") return Response.json({ job_id: "story-AIR-38", kind: "story" });
      if (url.startsWith(`${prefix}/`)) {
        const rest = url.slice(prefix.length + 1);
        const [id, tail] = rest.split("/");
        const row = server.threads.find((t) => t.id === decodeURIComponent(id));
        if (!row) return Response.json({ detail: "no such thread" }, { status: 404 });
        if (tail === "status") return Response.json(server.status);
        if (tail === "stop") return new Response(null, { status: 204 });
        if (tail === "stories" && method === "POST") {
          // a story card's started job, kept on the message whose `data-story` part it names
          const story = decodeURIComponent(rest.split("/")[2]);
          const part = { type: "data-job", id: `${story}-job`, data: { story, ...raw } };
          const held = (server.messages[row.id] ?? []).find((m) => m.parts.some((p) => p.type === "data-story" && p.id === story));
          if (!held) return Response.json({ error: "no such story in this chat" }, { status: 404 });
          held.parts.push(part);
          return Response.json(part, { status: 201 });
        }
        if (method === "DELETE") {
          server.threads = server.threads.filter((t) => t.id !== row.id);
          return new Response(null, { status: 204 });
        }
        return Response.json({ ...row, todos: [], messages: server.messages[row.id] ?? [] });
      }
      return Response.json({ detail: `unexpected ${method} ${url}` }, { status: 404 });
    }),
  );
  return server;
}
