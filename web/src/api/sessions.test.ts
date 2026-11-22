import { callOf, stubJson } from "@/test-utils/fetch-stub";
import {
  chatOptions,
  createSession,
  deleteSession,
  getSession,
  listPins,
  listRepositories,
  listSessions,
  pinRepository,
  sessionStatus,
  stopSession,
  toUIMessage,
  unpinRepository,
  updateSession,
} from "./sessions";
import type { StoredMessage } from "./sessions";

afterEach(() => vi.unstubAllGlobals());

const ROW = { id: "chat-1", kind: "plain", title: "", repo: null, base_ref: null, branch: null, running: false, created: 1, updated: 2 };

test("listSessions reads the chats, narrowed to a kind when asked", async () => {
  const fetchMock = stubJson([ROW]);
  expect(await listSessions()).toEqual([ROW]);
  await listSessions("repository");
  expect(callOf(fetchMock, 0)).toMatchObject({ url: "/v1/airlie/chat/threads", method: "GET" });
  expect(callOf(fetchMock, 1).url).toBe("/v1/airlie/chat/threads?kind=repository");
});

test("chatOptions reads the models and personas; updateSession PATCHes only what it is given", async () => {
  const options = { models: [{ id: "m", label: "M" }], personas: [{ id: "chat", label: "chat" }] };
  const fetchMock = stubJson(options);
  expect(await chatOptions()).toEqual(options);
  await updateSession("chat-1", { persona: "architect" });
  expect(callOf(fetchMock, 0)).toMatchObject({ url: "/v1/airlie/chat/options", method: "GET" });
  expect(callOf(fetchMock, 1)).toMatchObject({ url: "/v1/airlie/chat/threads/chat-1", method: "PATCH", body: { persona: "architect" } });
});

test("createSession posts the kind, repository and base", async () => {
  const fetchMock = stubJson(ROW, { status: 201 });
  expect(await createSession("repository", "acme/api", "main")).toEqual(ROW);
  expect(callOf(fetchMock)).toMatchObject({
    url: "/v1/airlie/chat/threads",
    method: "POST",
    body: { kind: "repository", repo: "acme/api", base_ref: "main" },
  });
});

test("createSession of a plain chat sends only its kind", async () => {
  const fetchMock = stubJson(ROW, { status: 201 });
  await createSession("plain");
  expect(callOf(fetchMock).body).toEqual({ kind: "plain" });
});

test("getSession, stopSession and deleteSession name the chat in the path", async () => {
  const fetchMock = stubJson({ ...ROW, todos: [], messages: [] });
  await getSession("chat-1");
  const stop = stubJson({}, { status: 204 });
  await stopSession("chat-1");
  const del = stubJson({}, { status: 204 });
  await deleteSession("chat-1");
  expect(callOf(fetchMock)).toMatchObject({ url: "/v1/airlie/chat/threads/chat-1", method: "GET" });
  expect(callOf(stop)).toMatchObject({ url: "/v1/airlie/chat/threads/chat-1/stop", method: "POST" });
  expect(callOf(del)).toMatchObject({ url: "/v1/airlie/chat/threads/chat-1", method: "DELETE" });
});

test("pins are read, put and deleted under owner/name", async () => {
  const read = stubJson({ pins: ["acme/api"] });
  expect(await listPins()).toEqual(["acme/api"]);
  expect(callOf(read).url).toBe("/v1/airlie/chat/pins");
  const put = stubJson({}, { status: 204 });
  await pinRepository("acme/api");
  expect(callOf(put)).toMatchObject({ url: "/v1/airlie/chat/pins/acme/api", method: "PUT" });
  const del = stubJson({}, { status: 204 });
  await unpinRepository("acme/api");
  expect(callOf(del)).toMatchObject({ url: "/v1/airlie/chat/pins/acme/api", method: "DELETE" });
});

test("listRepositories reads the names a person may pin", async () => {
  const fetchMock = stubJson({ repos: ["acme/api", "acme/web"] });
  expect(await listRepositories()).toEqual(["acme/api", "acme/web"]);
  expect(callOf(fetchMock).url).toBe("/v1/airlie/repos/names");
});

test("toUIMessage keeps the parts and moves usage and interrupted to metadata", () => {
  const stored: StoredMessage = {
    id: "m1",
    role: "assistant",
    parts: [{ type: "text", text: "hi" }, { type: "airlie_upload", upload: "abc", name: "a.png" }],
    created: 5,
    usage: { prompt_tokens: 10, completion_tokens: 2, cached_tokens: 0, max_context: 100 },
    interrupted: true,
  };
  expect(toUIMessage(stored)).toEqual({
    id: "m1",
    role: "assistant",
    parts: stored.parts,
    metadata: { created: 5, usage: stored.usage, interrupted: true },
  });
  expect(toUIMessage({ ...stored, usage: null, interrupted: false }).metadata).toEqual({ created: 5 });
});

test("sessionStatus reads the chat's status route", async () => {
  const status = { vm: null, engine: { name: "main", model: "base", state: "ready", footprint_gb: 10 }, memory: { used_gb: 10, budget_gb: 64, waiting: null }, branch: null };
  const fetchMock = stubJson(status);
  expect(await sessionStatus("chat 1")).toEqual(status);
  expect(callOf(fetchMock)).toMatchObject({ url: "/v1/airlie/chat/threads/chat%201/status", method: "GET" });
});
