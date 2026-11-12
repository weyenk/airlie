import { createPinia, setActivePinia } from "pinia";
import { onUnauthorized, setKey } from "@/api";
import type { StoredMessage, ThreadDetail, ThreadRow } from "@/api/sessions";
import { openStream, streamResponse, textChunks, turnChunks } from "@/test-utils/ui-stream";
import type { Change, FeedState } from "../feed";
import { useFeedStore } from "./feed";
import { REFUSED_MS, useTurns } from "./turns";

// The page's feed is the one seam faked here (the test is the server and sends the `chat` hints itself).
const feed = vi.hoisted(() => ({ opts: undefined as { onChange(c: Change): void; onState(s: FeedState): void } | undefined }));
vi.mock("../feed", () => ({
  connectFeed: (opts: { onChange(c: Change): void; onState(s: FeedState): void }) => {
    feed.opts = opts;
    return vi.fn();
  },
}));

const ROW: ThreadRow = {
  id: "chat-1",
  kind: "repository",
  title: "hi",
  repo: "acme/api",
  base_ref: "main",
  branch: "airlie/chat-abc",
  running: false,
  created: 1,
  updated: 2,
};
const PLAIN: ThreadRow = { ...ROW, id: "chat-2", kind: "plain", repo: null, base_ref: null, branch: null };

const USAGE = { prompt_tokens: 1000, completion_tokens: 234, cached_tokens: 0, max_context: 262144 };

const user = (id: string, text: string): StoredMessage => ({
  id,
  role: "user",
  parts: [{ type: "text", text }],
  created: 1,
  usage: null,
  interrupted: false,
});
const assistant = (id: string, parts: StoredMessage["parts"], over: Partial<StoredMessage> = {}): StoredMessage => ({
  id,
  role: "assistant",
  parts,
  created: 2,
  usage: null,
  interrupted: false,
  ...over,
});
const detail = (row: ThreadRow, messages: StoredMessage[] = [], over: Partial<ThreadDetail> = {}): ThreadDetail => ({
  ...row,
  todos: [],
  messages,
  ...over,
});

type Reply = Response | Promise<Response> | Error;
/** The server: history reads answer with `history()` (read each time), turns with `turn()`; every call is kept. */
function serve(opts: { history: () => ThreadDetail | Error; turn?: () => Reply; stop?: () => Reply }) {
  const calls: { url: string; method: string; body: unknown; headers: Record<string, string> }[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const raw = init?.body;
    calls.push({
      url,
      method: init?.method ?? "GET",
      body: typeof raw === "string" ? JSON.parse(raw) : undefined,
      headers: (init?.headers ?? {}) as Record<string, string>,
    });
    let reply: Reply;
    if (url === "/v1/airlie/chat/turn") reply = opts.turn!();
    else if (url.endsWith("/stop")) reply = opts.stop?.() ?? new Response(null, { status: 204 });
    else {
      const d = opts.history();
      reply = d instanceof Error ? d : Response.json(d);
    }
    if (reply instanceof Error) throw reply;
    return reply;
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, turns: () => calls.filter((c) => c.url === "/v1/airlie/chat/turn") };
}

beforeEach(() => {
  setActivePinia(createPinia());
  feed.opts = undefined;
  useFeedStore().start();
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  onUnauthorized(undefined);
});

const textOf = (m: { parts: { type: string; text?: string }[] }) =>
  m.parts
    .filter((p) => p.type === "text")
    .map((p) => p.text)
    .join("");

test("a text stream becomes one assistant message with its text", async () => {
  serve({ history: () => detail(ROW), turn: () => streamResponse(turnChunks("a1", textChunks("t1", "Hello there"))) });
  const turns = useTurns();
  const chat = turns.chatFor(ROW);
  await turns.send(ROW.id, { text: "hi" });
  expect(chat.messages.map((m) => [m.role, textOf(m)])).toEqual([
    ["user", "hi"],
    ["assistant", "Hello there"],
  ]);
  expect(turns.status(ROW.id)).toBe("ready");
  expect(turns.cut(ROW.id)).toBe(false);
});

test("finish usage sets the context used and its limit", async () => {
  serve({ history: () => detail(ROW), turn: () => streamResponse(turnChunks("a1", textChunks("t1", "ok"), USAGE)) });
  const turns = useTurns();
  turns.chatFor(ROW);
  expect(turns.usage(ROW.id)).toBeNull();
  await turns.send(ROW.id, { text: "hi" });
  expect(turns.usage(ROW.id)).toEqual({ used: 1234, limit: 262144 });
});

test("the request names the session kind repository and branch base and sends only the newest message", async () => {
  setKey("serve-key");
  const server = serve({ history: () => detail(ROW), turn: () => streamResponse(turnChunks("a1", textChunks("t1", "ok"))) });
  const turns = useTurns();
  turns.chatFor(ROW);
  await turns.send(ROW.id, { text: "first" });
  await turns.send(ROW.id, { text: "second", skill: { name: "story", action: "story" } });
  const [one, two] = server.turns();
  expect(one.body).toMatchObject({ id: "chat-1", kind: "repository", repo: "acme/api", base_ref: "main" });
  expect((one.body as { messages: unknown[] }).messages).toHaveLength(1);
  expect((two.body as { messages: { parts: unknown[] }[] }).messages).toHaveLength(1);
  expect((two.body as { messages: { parts: { text: string }[] }[] }).messages[0].parts[0].text).toBe("second");
  expect(two.body).toMatchObject({ skill: "story" });
  expect(one.headers.authorization).toBe("Bearer serve-key");
  expect(one.body).not.toHaveProperty("approvals");
});

test("the request of a plain chat names no repository", async () => {
  const server = serve({ history: () => detail(PLAIN), turn: () => streamResponse(turnChunks("a1", textChunks("t1", "ok"))) });
  const turns = useTurns();
  turns.chatFor(PLAIN);
  await turns.send(PLAIN.id, { text: "hi", attachments: [{ id: "f".repeat(32), name: "a.png", mime: "image/png", size: 3, kind: "image" }] });
  const body = server.turns()[0].body as { kind: string; repo?: string; messages: { parts: { type: string; url?: string }[] }[] };
  expect(body.kind).toBe("plain");
  expect(body.repo).toBeUndefined();
  expect(body.messages[0].parts.find((p) => p.type === "file")?.url).toBe(`/v1/airlie/uploads/${"f".repeat(32)}`);
});

test("a 401 from the turn reaches the sign-in redirect", async () => {
  const signedOut = vi.fn();
  onUnauthorized(signedOut);
  serve({ history: () => detail(ROW), turn: () => Response.json({ error: "not signed in" }, { status: 401 }) });
  const turns = useTurns();
  turns.chatFor(ROW);
  await turns.send(ROW.id, { text: "hi" });
  expect(signedOut).toHaveBeenCalled();
  expect(turns.status(ROW.id)).toBe("error");
});

test("a cut stream keeps the partial reply and offers retry", async () => {
  const stream = openStream();
  serve({ history: () => new Error("airlie is unreachable"), turn: () => stream.response });
  const turns = useTurns();
  const chat = turns.chatFor(ROW);
  const sent = turns.send(ROW.id, { text: "do it" });
  stream.push(
    { type: "start", messageId: "a1" },
    { type: "start-step" },
    ...textChunks("t1", "Working on"),
    { type: "tool-input-start", toolCallId: "c1", toolName: "shell" },
    { type: "tool-input-available", toolCallId: "c1", toolName: "shell", input: { cmd: "ls" } },
    { type: "tool-input-start", toolCallId: "c2", toolName: "git_push" },
    { type: "tool-input-available", toolCallId: "c2", toolName: "git_push", input: {} },
    { type: "tool-approval-request", approvalId: "ap1", toolCallId: "c2" },
  );
  stream.end({ done: false });
  await sent;
  await vi.waitFor(() => expect(turns.cut(ROW.id)).toBe(true));
  const reply = chat.messages[1];
  expect(textOf(reply)).toBe("Working on");
  const tools = reply.parts.filter((p) => p.type === "tool-shell" || p.type === "tool-git_push") as { state: string; errorText?: string }[];
  expect(tools.map((t) => t.state)).toEqual(["output-error", "output-error"]);
  expect(tools[0].errorText).toMatch(/interrupted/);
  expect(reply.parts.some((p) => (p as { state?: string }).state === "approval-requested")).toBe(false);
  expect(turns.canRetry(ROW.id)).toBe(true);
});

test("a cut while the server still runs waits for the chat hint then shows the saved reply", async () => {
  const stream = openStream();
  let saved = detail(ROW);
  const server = serve({ history: () => saved, turn: () => stream.response });
  const turns = useTurns();
  const chat = turns.chatFor(ROW);
  const sent = turns.send(ROW.id, { text: "do it" });
  stream.push({ type: "start", messageId: "a1" }, ...textChunks("t1", "Part"));
  await vi.waitFor(() => expect(textOf(chat.messages[1])).toBe("Part"));
  saved = detail(ROW, [user("u1", "do it")], { running: true });
  stream.drop();
  await sent;
  await vi.waitFor(() => expect(turns.cut(ROW.id)).toBe(true));
  const reads = () => server.calls.filter((c) => c.method === "GET" && c.url.startsWith("/v1/airlie/chat/threads/")).length;
  await vi.waitFor(() => expect(reads()).toBeGreaterThanOrEqual(2)); // the first read at open, the second after the cut
  const before = reads();
  expect(textOf(chat.messages[1])).toBe("Part"); // still the partial: the row says running
  saved = detail(ROW, [user("u1", "do it"), assistant("a1", [{ type: "text", text: "Part, then the rest" }])]);
  feed.opts?.onChange({ topic: "chat", key: null });
  await vi.waitFor(() => expect(textOf(chat.messages[1])).toBe("Part, then the rest"));
  expect(reads()).toBe(before + 1);
});

test("retry resends the message and the thread holds it once", async () => {
  const history = detail(ROW, [user("u1", "do it"), assistant("a1", [{ type: "text", text: "Par" }], { interrupted: true })]);
  const server = serve({ history: () => history, turn: () => streamResponse(turnChunks("a2", textChunks("t1", "Done"))) });
  const turns = useTurns();
  const chat = turns.chatFor(ROW);
  await turns.reload(ROW.id);
  expect(turns.canRetry(ROW.id)).toBe(true);
  await turns.retry(ROW.id);
  const body = server.turns()[0].body as { messages: { id: string; role: string }[] };
  expect(body.messages).toHaveLength(1);
  expect(body.messages[0]).toMatchObject({ id: "u1", role: "user" });
  expect(chat.messages.map((m) => [m.id, m.role, textOf(m)])).toEqual([
    ["u1", "user", "do it"],
    ["a2", "assistant", "Done"],
  ]);
});

test("answer sends the approval with no new message and the reply continues the asked message", async () => {
  const asked = assistant(
    "a1",
    [
      { type: "text", text: "I need to push." },
      { type: "tool-git_push", toolCallId: "c1", state: "approval-requested", input: {}, errorText: "needs a yes", approval: { id: "ap1" } },
      { type: "data-approval", id: "ap1", data: { id: "ap1", tool: "git_push", arguments: {} } },
    ],
    { interrupted: false },
  );
  const server = serve({
    history: () => detail(ROW, [user("u1", "push it"), asked]),
    turn: () =>
      streamResponse([
        { type: "start", messageId: "a1" },
        { type: "start-step" },
        { type: "tool-input-start", toolCallId: "c1-answered", toolName: "git_push" },
        { type: "tool-input-available", toolCallId: "c1-answered", toolName: "git_push", input: {} },
        { type: "tool-output-available", toolCallId: "c1-answered", output: "pushed" },
        ...textChunks("t2", "Pushed."),
        { type: "finish-step" },
        { type: "finish", messageMetadata: {} },
      ]),
  });
  const turns = useTurns();
  const chat = turns.chatFor(ROW);
  await turns.reload(ROW.id);
  await turns.answer(ROW.id, "ap1", true);
  const body = server.turns()[0].body as { approvals: unknown[]; messages: unknown[] };
  expect(body.approvals).toEqual([{ id: "ap1", approved: true }]);
  expect(body.messages).toEqual([]);
  expect(chat.messages.map((m) => m.id)).toEqual(["u1", "a1"]); // no new message: the reply went onto the asked one
  const types = chat.messages[1].parts.map((p) => `${p.type}:${(p as { state?: string }).state ?? ""}`);
  expect(types).toContain("tool-git_push:approval-responded");
  expect(types).toContain("tool-git_push:output-available"); // the server's run of the call
  expect(textOf(chat.messages[1])).toContain("Pushed.");
});

test("an answer the server refuses shows its reason on the prompt", async () => {
  const asked = assistant("a1", [
    { type: "tool-git_push", toolCallId: "c1", state: "approval-requested", input: {}, approval: { id: "ap1" } },
    { type: "data-approval", id: "ap1", data: { id: "ap1", tool: "git_push", arguments: {} } },
  ]);
  serve({
    history: () => detail(ROW, [user("u1", "push it"), asked]),
    turn: () => Response.json({ error: "no permission request of this chat waits for that answer" }, { status: 409 }),
  });
  const turns = useTurns();
  const chat = turns.chatFor(ROW);
  await turns.reload(ROW.id);
  expect(turns.refused(ROW.id, "ap1")).toBeNull();
  await turns.answer(ROW.id, "ap1", true);
  expect(turns.refused(ROW.id, "ap1")).toBe("no permission request of this chat waits for that answer");
  await vi.waitFor(() =>
    expect(chat.messages[1].parts.some((p) => (p as { state?: string }).state === "approval-requested")).toBe(true),
  ); // the saved thread's state is what shows
});

test("a refused answer's notice goes after a few seconds", async () => {
  const asked = assistant("a1", [
    { type: "tool-git_push", toolCallId: "c1", state: "approval-requested", input: {}, approval: { id: "ap1" } },
    { type: "data-approval", id: "ap1", data: { id: "ap1", tool: "git_push", arguments: {} } },
  ]);
  serve({
    history: () => detail(ROW, [user("u1", "push it"), asked]),
    turn: () => Response.json({ error: "this thread has a turn running; wait for it to finish" }, { status: 409 }),
  });
  const turns = useTurns();
  turns.chatFor(ROW);
  await turns.reload(ROW.id);
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  try {
    await turns.answer(ROW.id, "ap1", true);
    expect(turns.refused(ROW.id, "ap1")).toMatch(/turn running/);
    vi.advanceTimersByTime(REFUSED_MS);
    expect(turns.refused(ROW.id, "ap1")).toBeNull();
  } finally {
    vi.useRealTimers();
  }
});

test("an answer while the asking turn still streams sends nothing and leaves that stream alone", async () => {
  const stream = openStream();
  const server = serve({ history: () => detail(ROW), turn: () => stream.response });
  const turns = useTurns();
  turns.chatFor(ROW);
  const sent = turns.send(ROW.id, { text: "push it" });
  stream.push(
    { type: "start", messageId: "a1" },
    { type: "start-step" },
    { type: "tool-input-start", toolCallId: "c1", toolName: "git_push" },
    { type: "tool-input-available", toolCallId: "c1", toolName: "git_push", input: {} },
    { type: "tool-approval-request", approvalId: "ap1", toolCallId: "c1" },
    { type: "data-approval", id: "ap1", data: { id: "ap1", tool: "git_push", arguments: {} } },
    ...textChunks("t1", "I need your OK"),
  );
  await vi.waitFor(() => expect(turns.status(ROW.id)).toBe("streaming"));

  await turns.answer(ROW.id, "ap1", true);

  expect(server.turns()).toHaveLength(1);
  expect(turns.refused(ROW.id, "ap1")).toBeNull();
  expect(turns.status(ROW.id)).toBe("streaming");
  stream.end({ done: false }); // the first stream's cut is still seen: its scan was not reset
  await sent;
  await vi.waitFor(() => expect(turns.cut(ROW.id)).toBe(true));
});

test("reconnect while it settles starts no second wait", async () => {
  let saved = detail(ROW, [user("u1", "do it")], { running: true });
  const server = serve({ history: () => saved });
  const turns = useTurns();
  turns.chatFor(ROW);
  await turns.reload(ROW.id);
  const reads = () => server.calls.filter((c) => c.method === "GET").length;
  const before = reads();
  const first = turns.reconnect(ROW.id);
  const second = turns.reconnect(ROW.id);
  await vi.waitFor(() => expect(turns.settling(ROW.id)).toBe(true));
  await vi.waitFor(() => expect(reads()).toBe(before + 1));
  saved = detail(ROW, [user("u1", "do it"), assistant("a1", [{ type: "text", text: "done" }])]);
  await vi.waitFor(() => {
    feed.opts?.onChange({ topic: "chat", key: null }); // until the loop waits on it
    expect(turns.settling(ROW.id)).toBe(false);
  });
  await Promise.all([first, second]);
  expect(reads()).toBe(before + 2);
  expect(turns.settling(ROW.id)).toBe(false);
});

test("messagesOf reads a chat without making one, and follows the chat once made", async () => {
  serve({ history: () => detail(ROW, [user("u1", "hi")]) });
  const turns = useTurns();
  expect(turns.messagesOf(ROW.id)).toEqual([]);
  expect(turns.status(ROW.id)).toBe("ready");
  turns.chatFor(ROW);
  await turns.reload(ROW.id);
  expect(turns.messagesOf(ROW.id).map((m) => m.id)).toEqual(["u1"]);
});

test("addPart puts a saved part on its message", async () => {
  serve({ history: () => detail(ROW, [user("u1", "hi"), assistant("a1", [{ type: "text", text: "story" }])]) });
  const turns = useTurns();
  const chat = turns.chatFor(ROW);
  await turns.reload(ROW.id);
  turns.addPart(ROW.id, "a1", { type: "data-job", id: "s1-job", data: { story: "s1", job_id: "story-AIR-1", kind: "story" } });
  expect(chat.messages[1].parts.map((p) => p.type)).toEqual(["text", "data-job"]);
});

test("a data engine chunk is the waiting line until text arrives", async () => {
  const stream = openStream();
  serve({ history: () => detail(ROW), turn: () => stream.response });
  const turns = useTurns();
  turns.chatFor(ROW);
  const sent = turns.send(ROW.id, { text: "hi" });
  expect(turns.waiting(ROW.id)).toBeNull();
  stream.push(
    { type: "start", messageId: "a1" },
    { type: "data-engine", data: { engine: "gemma", state: "downloading", detail: "mlx-community/gemma", progress: null }, transient: true },
  );
  await vi.waitFor(() => expect(turns.waiting(ROW.id)).toMatchObject({ name: "gemma", state: "downloading", detail: "mlx-community/gemma" }));
  stream.push(...textChunks("t1", "Hi"));
  await vi.waitFor(() => expect(turns.waiting(ROW.id)).toBeNull());
  stream.push({ type: "finish", messageMetadata: {} });
  stream.end();
  await sent;
});

test("stop posts the stop route and ends the stream", async () => {
  const stream = openStream();
  const server = serve({
    history: () => detail(ROW),
    turn: () => stream.response,
    stop: () => {
      stream.end({ done: false }); // the server ends the stream as it saves the interrupted reply
      return new Response(null, { status: 204 });
    },
  });
  const turns = useTurns();
  turns.chatFor(ROW);
  const sent = turns.send(ROW.id, { text: "hi" });
  stream.push({ type: "start", messageId: "a1" }, ...textChunks("t1", "Partly"));
  await vi.waitFor(() => expect(turns.status(ROW.id)).toBe("streaming"));
  await turns.stop(ROW.id);
  await sent;
  expect(server.calls.some((c) => c.method === "POST" && c.url === "/v1/airlie/chat/threads/chat-1/stop")).toBe(true);
  expect(turns.status(ROW.id)).toBe("ready");
  expect(turns.cut(ROW.id)).toBe(false); // a Stop is not a cut
});

test("a reloaded chat shows its stored history and its last usage", async () => {
  serve({
    history: () =>
      detail(ROW, [user("u1", "hi"), assistant("a1", [{ type: "text", text: "Hello" }], { usage: USAGE })], {
        todos: [{ id: "t1", text: "x", status: "pending" }],
      }),
  });
  const turns = useTurns();
  const chat = turns.chatFor(ROW);
  await turns.reload(ROW.id);
  expect(chat.messages.map((m) => [m.role, textOf(m)])).toEqual([
    ["user", "hi"],
    ["assistant", "Hello"],
  ]);
  expect(turns.usage(ROW.id)).toEqual({ used: 1234, limit: 262144 });
  expect(turns.chatFor(ROW)).toBe(chat); // one Chat per session
  expect(turns.todos(ROW.id)).toEqual([{ id: "t1", text: "x", status: "pending" }]);
});

test("a turn's error chunk is an error, not a cut", async () => {
  serve({
    history: () => detail(ROW),
    turn: () => streamResponse([{ type: "start", messageId: "a1" }, { type: "error", errorText: "the engine failed" }]),
  });
  const turns = useTurns();
  turns.chatFor(ROW);
  await turns.send(ROW.id, { text: "hi" });
  expect(turns.status(ROW.id)).toBe("error");
  expect(turns.cut(ROW.id)).toBe(false);
  expect(turns.error(ROW.id)?.message).toBe("the engine failed");
});

test("retry keeps the skill of the turn it resends", async () => {
  const server = serve({ history: () => detail(ROW), turn: () => streamResponse(turnChunks("a1", textChunks("t1", "ok"))) });
  const turns = useTurns();
  turns.chatFor(ROW);
  await turns.send(ROW.id, { text: "/story x", sent: "x", skill: { name: "story", action: "story" } });
  await turns.retry(ROW.id);
  expect(server.turns()[1].body).toMatchObject({ skill: "story" });
});

test("forget drops the session's chat so the next one starts from the server", async () => {
  serve({ history: () => detail(ROW, [user("u1", "hi")]) });
  const turns = useTurns();
  const chat = turns.chatFor(ROW);
  turns.forget(ROW.id);
  expect(turns.status(ROW.id)).toBe("ready");
  expect(turns.cut(ROW.id)).toBe(false);
  expect(turns.chatFor(ROW)).not.toBe(chat);
});

test("a server reasoning part is kept in the message before its text", async () => {
  serve({
    history: () => detail(ROW),
    turn: () =>
      streamResponse(
        turnChunks("a1", [
          { type: "reasoning-start", id: "r1" },
          { type: "reasoning-delta", id: "r1", delta: "thinking it over" },
          { type: "reasoning-end", id: "r1" },
          ...textChunks("t1", "Answer"),
        ]),
      ),
  });
  const turns = useTurns();
  const chat = turns.chatFor(ROW);
  await turns.send(ROW.id, { text: "hi" });
  const parts = chat.messages[1].parts as { type: string; text?: string }[];
  expect(parts.filter((p) => p.type !== "step-start").map((p) => [p.type, p.text])).toEqual([
    ["reasoning", "thinking it over"],
    ["text", "Answer"],
  ]);
});

test("a tool call whose arguments did not parse shows its error on the tool part", async () => {
  serve({
    history: () => detail(ROW),
    turn: () =>
      streamResponse(
        turnChunks("a1", [
          { type: "tool-input-start", toolCallId: "c1", toolName: "shell" },
          { type: "tool-input-error", toolCallId: "c1", toolName: "shell", input: "{oops", errorText: "arguments are not JSON" },
        ]),
      ),
  });
  const turns = useTurns();
  const chat = turns.chatFor(ROW);
  await turns.send(ROW.id, { text: "hi" });
  const tool = chat.messages[1].parts.find((p) => p.type === "tool-shell") as { state: string; errorText: string };
  expect(tool).toMatchObject({ state: "output-error", errorText: "arguments are not JSON" });
});


test("running says whether the server still runs the turn, as the last history read said", async () => {
  let running = true;
  serve({ history: () => detail(ROW, [user("u1", "hi")], { running }) });
  const turns = useTurns();
  turns.chatFor(ROW);
  expect(turns.running(ROW.id)).toBe(false);
  await turns.reload(ROW.id);
  expect(turns.running(ROW.id)).toBe(true);
  running = false;
  await turns.reload(ROW.id);
  expect(turns.running(ROW.id)).toBe(false);
});
