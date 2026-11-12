import { flushPromises, mount } from "@vue/test-utils";
import { openChat, message, ROW, startThread } from "@/test-utils/thread";
import { streamResponse, textChunks, turnChunks } from "@/test-utils/ui-stream";
import CutLine from "./CutLine.vue";

const user = message("u1", "user", [{ type: "text", text: "hi" }]);
const show = async (running: boolean) => {
  const server = startThread([user]);
  await openChat();
  server.calls.length = 0;
  return { server, w: mount(CutLine, { props: { sessionId: ROW.id, running } }) };
};
const button = (w: Awaited<ReturnType<typeof show>>["w"], name: string) => w.findAll("button").find((b) => b.text() === name)!;

afterEach(() => vi.unstubAllGlobals());

test("a cut turn the server still runs offers reconnect and stop turn", async () => {
  const { w, server } = await show(true);
  expect(w.text()).toContain("Stream cut. The turn kept running on the server and is saved");
  await button(w, "Reconnect").trigger("click");
  await flushPromises();
  expect(server.calls.some((c) => c.method === "GET" && c.url === `/v1/airlie/chat/threads/${ROW.id}`)).toBe(true);
  await button(w, "Stop turn").trigger("click");
  await flushPromises();
  expect(server.calls.some((c) => c.url.endsWith("/stop") && c.method === "POST")).toBe(true);
});

test("reconnect is disabled while it waits for the saved reply", async () => {
  const { w, server } = await show(true);
  server.detail = { ...server.detail, running: true };
  await button(w, "Reconnect").trigger("click");
  await flushPromises();
  expect(button(w, "Reconnect").attributes("disabled")).toBeDefined();
  expect(button(w, "Stop turn").attributes("disabled")).toBeUndefined();
});

test("a cut reply shows cut off with retry", async () => {
  const { w, server } = await show(false);
  expect(w.text()).toContain("Cut off");
  expect(w.text()).not.toContain("Reconnect");
  server.turn = () => streamResponse(turnChunks("a1", textChunks("t1", "ok")));
  await button(w, "Retry").trigger("click");
  await flushPromises();
  expect(server.calls.find((c) => c.url === "/v1/airlie/chat/turn")?.body).toMatchObject({ id: ROW.id });
});
