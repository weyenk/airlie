// Test helper: a server for the chat row's status chip: the engines list (live through the store) and a chat's status route.

import type { EngineStatus } from "@/api/engines";
import type { SessionStatusData, StoredMessage } from "@/api/sessions";
import { ROW } from "./thread";

export const engine = (over: Partial<EngineStatus> = {}): EngineStatus => ({
  name: "gemma",
  kind: "engine",
  state: "ready",
  detail: "",
  progress: null,
  holders: [],
  lifecycle: "on_demand",
  roles: ["chat"],
  ...over,
});

export const vmRow = (over: Partial<EngineStatus> = {}): EngineStatus =>
  engine({ name: "vm:chat-1", kind: "vm", lifecycle: "on_demand", roles: [], ...over });

export const STATUS: SessionStatusData = {
  vm: { name: "vm:chat-1", state: "ready", detail: "", since: 0, memory_gb: 8 },
  engine: { name: "gemma", model: "gemma4", label: "Gemma 4", state: "ready", footprint_gb: 29.6 },
  memory: { used_gb: 41, budget_gb: 64, waiting: null },
  branch: { name: "airlie/chat-abc", base: "main", ahead: 3, pushed: false },
  ask_first: ["push_branch", "open_pr", "build_image"],
};

export type StatusServer = {
  engines: EngineStatus[];
  status: SessionStatusData;
  /** The status route answers 500. */
  down: boolean;
  /** The saved history of the chat `ROW` (its replies carry the usage the context readout reads). */
  messages: StoredMessage[];
  calls: string[];
};

/** `fetch` answers the engines list and a chat's status route from the returned object (change it between steps). */
export function statusServer(init: Partial<StatusServer> = {}): StatusServer {
  const server: StatusServer = { engines: [engine()], status: STATUS, down: false, messages: [], calls: [], ...init };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/v1/events") return new Promise<Response>(() => {});
      server.calls.push(url);
      if (url === `/v1/airlie/chat/threads/${ROW.id}`) return Response.json({ ...ROW, todos: [], messages: server.messages });
      if (url === "/v1/airlie/engines") return Response.json(server.engines);
      if (url.endsWith("/status")) return server.down ? Response.json({ detail: "status is down" }, { status: 500 }) : Response.json(server.status);
      return Response.json({ detail: `unexpected ${url}` }, { status: 404 });
    }),
  );
  return server;
}
