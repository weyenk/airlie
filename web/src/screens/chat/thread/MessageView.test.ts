import { flushPromises, mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import type { AirlieUIMessage } from "@/api/sessions";
import { message, openChat, ROW, startThread } from "@/test-utils/thread";
import { openStream, textChunks } from "@/test-utils/ui-stream";
import MessageView from "./MessageView.vue";

const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:rest(.*)*", component: { template: "<i/>" } }] });
const view = (m: AirlieUIMessage, last = false) =>
  mount(MessageView, { props: { message: m, sessionId: ROW.id, last }, global: { plugins: [router] } });
const ui = (parts: unknown[], role: "assistant" | "user" = "assistant"): AirlieUIMessage => ({ id: "m1", role, parts: parts as never });

afterEach(() => vi.unstubAllGlobals());

test("a text part renders safe markdown", async () => {
  startThread();
  await openChat();
  const w = view(ui([{ type: "text", text: "**Done** <script>alert(1)</script> <img src=x onerror=alert(1)>" }]));
  expect(w.get("strong").text()).toBe("Done");
  expect(w.find("script").exists()).toBe(false);
  expect(w.find("img").exists()).toBe(false);
  expect(w.text()).toContain("<script>alert(1)</script>");
});

test("an unknown part renders nothing", async () => {
  startThread();
  await openChat();
  const w = view(ui([{ type: "step-start" }, { type: "source-url", sourceId: "s", url: "http://x" }, { type: "data-mystery", data: 1 }]));
  expect(w.text()).toBe("");
  expect(w.element.querySelectorAll("button")).toHaveLength(0);
});

test("a message is its parts in order: reasoning, tool, text", async () => {
  startThread();
  await openChat();
  const w = view(
    ui([
      { type: "reasoning", text: "think", state: "done" },
      { type: "tool-read_file", toolCallId: "c1", state: "output-available", input: { path: "a.py" }, output: "x" },
      { type: "text", text: "All read." },
    ]),
  );
  const text = w.text();
  expect(text.indexOf("Reasoning")).toBeLessThan(text.indexOf("read_file"));
  expect(text.indexOf("read_file")).toBeLessThan(text.indexOf("All read."));
});

test("only the newest todos part renders", async () => {
  startThread();
  await openChat();
  const todos = (text: string) => ({ type: "data-todos", data: [{ id: "1", text, status: "pending" }] });
  const w = view(ui([todos("old plan"), { type: "text", text: "go" }, todos("new plan")]));
  expect(w.text()).toContain("new plan");
  expect(w.text()).not.toContain("old plan");
  expect(w.findAll("ul")).toHaveLength(1);
});

test("an interrupted message reads its open call as interrupted", async () => {
  startThread();
  await openChat();
  const m = { ...ui([{ type: "tool-shell", toolCallId: "c1", state: "input-available", input: { command: "make" } }]), metadata: { interrupted: true } };
  expect(view(m as AirlieUIMessage).text()).toContain("interrupted");
});

test("a pending prompt survives a reload: the saved thread shows it with its buttons, answered ones read Approved", async () => {
  const approval = (id: string, extra: object = {}) => ({ type: "data-approval", id, data: { id, tool: "push_branch", arguments: { branch: "airlie/chat-ab12" }, ...extra } });
  startThread([
    message("u1", "user", [{ type: "text", text: "push it" }]),
    message("a1", "assistant", [
      { type: "tool-push_branch", toolCallId: "c1", state: "approval-requested", input: {}, approval: { id: "ap1" } },
      approval("ap1"),
      { type: "tool-push_branch", toolCallId: "c2", state: "approval-responded", input: {}, approval: { id: "ap2", approved: true } },
      approval("ap2", { answer: "approved" }),
    ]),
  ]);
  const turns = await openChat();
  const reply = turns.chatFor(ROW).messages[1];
  const w = view(reply);
  expect(w.findAll("button").filter((b) => b.text() === "Approve")).toHaveLength(1);
  expect(w.text()).toContain("Approved");
});

test("a prompt answered on this page reads Approved from the call's own state", async () => {
  const reply = ui([
    { type: "tool-push_branch", toolCallId: "c1", state: "approval-responded", input: {}, approval: { id: "ap1", approved: false } },
    { type: "data-approval", id: "ap1", data: { id: "ap1", tool: "push_branch", arguments: {} } },
  ]);
  startThread();
  await openChat();
  const w = view(reply);
  expect(w.text()).toContain("Denied");
  expect(w.findAll("button").filter((b) => b.text() === "Approve")).toHaveLength(0);
});

test("a waiting turn shows the engine line inline under the turn that waits", async () => {
  const server = startThread();
  const stream = openStream();
  server.turn = () => stream.response;
  const turns = await openChat();
  void turns.send(ROW.id, { text: "hi" });
  stream.push(
    { type: "start", messageId: "a1" },
    { type: "data-engine", data: { engine: "gemma", state: "waiting", detail: "waiting for memory", progress: null }, transient: true },
  );
  await vi.waitFor(() => expect(turns.waiting(ROW.id)).not.toBeNull());
  const w = view(turns.chatFor(ROW).messages.at(-1)!, true);
  expect(w.get("[role=status]").text()).toBe("Waiting for memory");
  expect(w.findAll("button").map((b) => b.text())).toEqual(expect.arrayContaining(["Wait", "Cancel"]));
  expect(view(turns.chatFor(ROW).messages[0], false).find("[role=status]").exists()).toBe(false);
  stream.end();
  await flushPromises();
});

test("a cut turn the server still runs says the stream was cut", async () => {
  const server = startThread([message("u1", "user", [{ type: "text", text: "hi" }])], { running: true });
  const stream = openStream();
  server.turn = () => stream.response;
  const turns = await openChat();
  void turns.send(ROW.id, { text: "again" });
  stream.push({ type: "start", messageId: "a1" }, { type: "start-step" }, ...textChunks("t1", "par"));
  await vi.waitFor(() => expect(turns.status(ROW.id)).toBe("streaming"));
  stream.drop();
  await vi.waitFor(() => expect(turns.cut(ROW.id)).toBe(true));
  await vi.waitFor(() => expect(turns.running(ROW.id)).toBe(true));
  const w = view(turns.chatFor(ROW).messages.at(-1)!, true);
  expect(w.text()).toContain("Stream cut. The turn kept running on the server and is saved");
});
