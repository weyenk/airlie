import { flushPromises, mount } from "@vue/test-utils";
import type { ApprovalData } from "@/api/sessions";
import { openChat, message, ROW, startThread } from "@/test-utils/thread";
import { openStream, streamResponse, textChunks, turnChunks } from "@/test-utils/ui-stream";
import PermissionPrompt from "./PermissionPrompt.vue";

const PUSH: ApprovalData = {
  id: "ap1",
  tool: "push_branch",
  arguments: { branch: "airlie/chat-ab12", commits: ["a1b2c3 Add the title route", "d4e5f6 Test it"] },
};
const asked = [
  message("u1", "user", [{ type: "text", text: "push it" }]),
  message("a1", "assistant", [
    { type: "tool-push_branch", toolCallId: "c1", state: "approval-requested", input: {}, approval: { id: "ap1" } },
    { type: "data-approval", id: "ap1", data: PUSH },
  ]),
];

async function show(approval: ApprovalData = PUSH) {
  const server = startThread(asked);
  await openChat();
  return { server, w: mount(PermissionPrompt, { props: { sessionId: ROW.id, approval } }) };
}
const button = (w: Awaited<ReturnType<typeof show>>["w"], name: string) => w.findAll("button").find((b) => b.text() === name)!;

afterEach(() => vi.unstubAllGlobals());

test("push_branch asks its question with the branch and its commits", async () => {
  const { w } = await show();
  expect(w.text()).toContain("Push session branch to origin?");
  expect(w.text()).toContain("airlie/chat-ab12");
  expect(w.text()).toContain("a1b2c3 Add the title route");
  expect(button(w, "Approve")).toBeDefined();
  expect(button(w, "Deny")).toBeDefined();
});

test("open_pr and build_image ask from their arguments", async () => {
  const pr = await show({ id: "ap2", tool: "open_pr", arguments: { title: "Title route", branch: "airlie/chat-ab12", base: "main" } });
  expect(pr.w.text()).toContain("Open a pull request?");
  expect(pr.w.text()).toContain("Title route");
  const image = await show({ id: "ap3", tool: "build_image", arguments: { key: "node-22" } });
  expect(image.w.text()).toContain("Build the image?");
  expect(image.w.text()).toContain("node-22");
});

test("approve and deny are buttons that send the answer", async () => {
  const { w, server } = await show();
  server.turn = () => streamResponse(turnChunks("a1", textChunks("t1", "Pushed.")));
  await button(w, "Approve").trigger("click");
  await flushPromises();
  expect(server.calls.find((c) => c.url === "/v1/airlie/chat/turn")?.body).toMatchObject({ approvals: [{ id: "ap1", approved: true }], messages: [] });
  const denied = await show();
  denied.server.turn = () => streamResponse(turnChunks("a1", textChunks("t1", "Fine.")));
  await button(denied.w, "Deny").trigger("click");
  await flushPromises();
  expect(denied.server.calls.find((c) => c.url === "/v1/airlie/chat/turn")?.body).toMatchObject({ approvals: [{ id: "ap1", approved: false }] });
});

test("once one is pressed the prompt reads the answer and offers no second one", async () => {
  const { w, server } = await show();
  server.turn = () => new Promise<Response>(() => {});
  await button(w, "Approve").trigger("click");
  expect(w.findAll("button")).toHaveLength(0);
  expect(w.get("[role=status]").text()).toBe("Approved");
});

test("a build_image prompt reads Approved while its own approved reply streams", async () => {
  const BUILD: ApprovalData = { id: "ap3", tool: "build_image", arguments: { key: "node-22" } };
  const { w, server } = await show(BUILD);
  const stream = openStream();
  server.turn = () => stream.response;
  await button(w, "Approve").trigger("click");
  stream.push({ type: "start", messageId: "a1" }, { type: "start-step" }, ...textChunks("t1", "Building"));
  await flushPromises();
  expect(w.text()).not.toContain("Answer when the reply ends");
  expect(w.get("[role=status]").text()).toBe("Approved");
  stream.push({ type: "finish-step" }, { type: "finish", messageMetadata: {} });
  stream.end();
  await flushPromises();
  expect(w.get("[role=status]").text()).toBe("Approved");
});

test("a pending prompt survives a reload and an answered one reads Approved or Denied", async () => {
  const { w } = await show();
  expect(button(w, "Approve").attributes("disabled")).toBeUndefined();
  const approved = mount(PermissionPrompt, { props: { sessionId: ROW.id, approval: { ...PUSH, answer: "approved" } } });
  expect(approved.text()).toContain("Approved");
  expect(approved.findAll("button")).toHaveLength(0);
  const denied = mount(PermissionPrompt, { props: { sessionId: ROW.id, approval: { ...PUSH, answer: "denied" } } });
  expect(denied.text()).toContain("Denied");
});

test("an approved call that failed reads failed with its error", async () => {
  const { w } = await show({ ...PUSH, answer: "failed", error: "error: remote rejected the push" });
  expect(w.text()).toContain("failed");
  expect(w.text()).toContain("remote rejected the push");
  expect(w.text()).not.toContain("Approved");
  expect(w.findAll("button")).toHaveLength(0);
});

test("a refused answer shows the servers reason", async () => {
  const { w, server } = await show();
  server.turn = () => Response.json({ error: "no permission request of this chat waits for that answer" }, { status: 409 });
  await button(w, "Approve").trigger("click");
  await flushPromises();
  expect(w.text()).toContain("no permission request of this chat waits for that answer");
  expect(button(w, "Approve").attributes("disabled")).toBeUndefined(); // beside the buttons, never instead of them
  expect(button(w, "Deny").attributes("disabled")).toBeUndefined();
});

test("while the asking turn still streams the buttons wait, and are enabled when it finishes", async () => {
  const server = startThread([]);
  const stream = openStream();
  server.turn = () => stream.response;
  const turns = await openChat();
  const sent = turns.send(ROW.id, { text: "push it" });
  stream.push(
    { type: "start", messageId: "a1" },
    { type: "start-step" },
    { type: "data-approval", id: "ap1", data: PUSH },
    ...textChunks("t1", "I need your OK"),
  );
  await vi.waitFor(() => expect(turns.status(ROW.id)).toBe("streaming"));
  const w = mount(PermissionPrompt, { props: { sessionId: ROW.id, approval: PUSH } });

  expect(button(w, "Approve").attributes("disabled")).toBeDefined();
  expect(button(w, "Deny").attributes("disabled")).toBeDefined();
  expect(w.text()).toContain("Answer when the reply ends");
  await button(w, "Approve").trigger("click");
  expect(server.calls.filter((c) => c.url === "/v1/airlie/chat/turn")).toHaveLength(1);

  stream.push({ type: "finish-step" }, { type: "finish", messageMetadata: {} });
  stream.end();
  await sent;
  await flushPromises();
  expect(button(w, "Approve").attributes("disabled")).toBeUndefined();
  expect(w.text()).not.toContain("Answer when the reply ends");
});

test("an expired request shows no buttons", async () => {
  const { w } = await show({ ...PUSH, expired: true });
  expect(w.text()).toContain("This request expired; ask again");
  expect(w.findAll("button")).toHaveLength(0);
});
