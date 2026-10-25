import { createPinia, setActivePinia } from "pinia";
import { ApiError } from "@/api";
import { StreamCut } from "@/api/chat";
import { HISTORY, useChatStore } from "./chat";

const api = vi.hoisted(() => ({ stream: vi.fn() }));
vi.mock("@/api/chat", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/chat")>()), ...api }));

/** A thread exactly as today's app writes it (src/airlie/serve/web/screens/chat.js). */
const TODAYS = [
  { role: "user", text: "/story add a button", sent: "add a button" },
  { role: "chat", text: "Add a button\n\nDone when it clicks.", streaming: false, thinking: true, action: "story", storyText: "Edited" },
  { role: "chat", text: "", streaming: false, thinking: false, action: "", started: { job_id: "story-1", kind: "story", label: "airlie · story" } },
  { role: "user", text: "look", sent: "look", attachments: [{ id: "u1", name: "shot.png", mime: "image/png", size: 10, kind: "image" }] },
];

function fresh() {
  setActivePinia(createPinia());
  return useChatStore();
}

beforeEach(() => {
  sessionStorage.clear();
  api.stream.mockReset().mockImplementation(async (_p: string, _b: unknown, onDelta: (t: string) => void) => {
    onDelta("Hel");
    onDelta("lo");
    return "Hello";
  });
});

test("a thread written by today's app loads, fields and all", () => {
  sessionStorage.setItem("airlie.chat", JSON.stringify(TODAYS));
  sessionStorage.setItem("airlie.chat.draft", JSON.stringify("half a thought"));
  const chat = fresh();
  expect(chat.thread).toEqual(TODAYS);
  expect(chat.draft).toBe("half a thought");
});

test("a reply that was still streaming when the page went loads as cut, not as writing for ever", () => {
  sessionStorage.setItem("airlie.chat", JSON.stringify([{ role: "user", text: "hi", sent: "hi" }, { role: "chat", text: "", streaming: true, thinking: false, action: "" }]));
  const chat = fresh();
  expect(chat.thread[1]).toMatchObject({ streaming: false, cut: true });
});

test("a thread that is not a list, or is not JSON, is an empty thread", () => {
  sessionStorage.setItem("airlie.chat", JSON.stringify({ not: "a list" }));
  expect(fresh().thread).toEqual([]);
  sessionStorage.setItem("airlie.chat", "{nope");
  expect(fresh().thread).toEqual([]);
});

test("sending streams the reply into the thread and saves it in today's format", async () => {
  const chat = fresh();
  await chat.send({ text: "hi there" });
  expect(chat.thread).toEqual([
    { role: "user", text: "hi there", sent: "hi there" },
    { role: "chat", text: "Hello", streaming: false, thinking: false, action: "" },
  ]);
  expect(JSON.parse(sessionStorage.getItem("airlie.chat")!)).toEqual(chat.thread);
  const [path, body] = api.stream.mock.calls[0];
  expect(path).toBe("/v1/chat/completions");
  expect(body).toEqual({ model: "chat", messages: [{ role: "user", content: "hi there" }], stream: true });
  expect(chat.busy).toBeNull();
});

test("the reply shows the words that have arrived while it streams", async () => {
  const chat = fresh();
  let release!: () => void;
  api.stream.mockImplementation(async (_p: string, _b: unknown, onDelta: (t: string) => void) => {
    onDelta("par");
    await new Promise<void>((r) => (release = r));
    return "par";
  });
  const sending = chat.send({ text: "q" });
  await Promise.resolve();
  expect(chat.thread[1]).toMatchObject({ text: "par", streaming: true });
  expect(chat.busy).not.toBeNull();
  release();
  await sending;
  expect(chat.thread[1].streaming).toBe(false);
});

test("a reasoning delta marks the reply as thinking", async () => {
  const chat = fresh();
  api.stream.mockImplementation(async (_p: string, _b: unknown, _d: unknown, opts: { onReasoning: (t: string) => void }) => {
    opts.onReasoning("hm");
    return "";
  });
  await chat.send({ text: "q" });
  expect(chat.thread[1].thinking).toBe(true);
});

test("a cut stream keeps what arrived and says so", async () => {
  const chat = fresh();
  api.stream.mockImplementation(async (_p: string, _b: unknown, onDelta: (t: string) => void) => {
    onDelta("half an ans");
    throw new StreamCut("half an ans");
  });
  await chat.send({ text: "q" });
  expect(chat.thread[1]).toMatchObject({ text: "half an ans", cut: true, streaming: false });
  expect(JSON.parse(sessionStorage.getItem("airlie.chat")!)[1]).toMatchObject({ text: "half an ans", cut: true });
});

test("a failed request is an error on the reply, not an exception", async () => {
  const chat = fresh();
  api.stream.mockRejectedValue(new ApiError(502, "engine down"));
  await chat.send({ text: "q" });
  expect(chat.thread[1]).toMatchObject({ error: "Can't reach airlie: engine down", streaming: false });
  expect(chat.thread[1].cut).toBeUndefined();
});

test("a 401 takes the turn back and puts what was typed into the draft", async () => {
  const chat = fresh();
  api.stream.mockRejectedValue(new ApiError(401, "no"));
  await chat.send({ text: "keep me" });
  expect(chat.thread).toEqual([]);
  expect(chat.draft).toBe("keep me");
  expect(JSON.parse(sessionStorage.getItem("airlie.chat.draft")!)).toBe("keep me");
});

test("a skill sends its name and only its request to the model; the thread keeps what was typed", async () => {
  const chat = fresh();
  await chat.send({ text: "/story a button", sent: "a button", skill: { name: "story", action: "story" } });
  expect(api.stream.mock.calls[0][1]).toEqual({ model: "chat", messages: [{ role: "user", content: "a button" }], stream: true, skill: "story" });
  expect(chat.thread[0]).toEqual({ role: "user", text: "/story a button", sent: "a button" });
  expect(chat.thread[1]).toMatchObject({ action: "story" });
});

test("attachments go as airlie_upload parts after the text", async () => {
  const chat = fresh();
  const a = { id: "u1", name: "shot.png", size: 3, kind: "image" };
  await chat.send({ text: "look", attachments: [a] });
  expect(api.stream.mock.calls[0][1].messages).toEqual([
    { role: "user", content: [{ type: "text", text: "look" }, { type: "airlie_upload", upload: "u1", name: "shot.png" }] },
  ]);
});

test("only the last 20 turns that have text are sent back", async () => {
  const chat = fresh();
  for (let i = 0; i < 15; i++) await chat.send({ text: `q${i}` });
  const messages = api.stream.mock.calls.at(-1)![1].messages;
  expect(messages).toHaveLength(HISTORY);
  expect(messages.at(-1)).toEqual({ role: "user", content: "q14" });
});

test("clear empties the thread and the draft without asking the model", async () => {
  const chat = fresh();
  await chat.send({ text: "q" });
  chat.setDraft("half");
  api.stream.mockClear();
  chat.clear();
  expect(chat.thread).toEqual([]);
  expect(chat.draft).toBe("");
  expect(JSON.parse(sessionStorage.getItem("airlie.chat")!)).toEqual([]);
  expect(api.stream).not.toHaveBeenCalled();
});

test("a second message while a reply streams is ignored", async () => {
  const chat = fresh();
  let release!: () => void;
  api.stream.mockImplementation(async () => {
    await new Promise<void>((r) => (release = r));
    return "";
  });
  const first = chat.send({ text: "one" });
  await chat.send({ text: "two" });
  expect(chat.thread.map((m) => m.text)).toEqual(["one", ""]);
  release();
  await first;
});
