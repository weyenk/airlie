import { flushPromises, mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import type { EngineStatus } from "@/api/engines";
import { openChat, ROW, startThread } from "@/test-utils/thread";
import WaitingLine from "./WaitingLine.vue";

const engine = (over: Partial<EngineStatus>): EngineStatus => ({
  name: "gemma",
  kind: "engine",
  state: "waiting",
  detail: "waiting for memory: held by reviewer",
  progress: null,
  holders: [],
  lifecycle: "",
  roles: [],
  ...over,
});
const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:rest(.*)*", component: { template: "<i/>" } }] });
const show = async (status: EngineStatus) => {
  const server = startThread();
  await openChat();
  const w = mount(WaitingLine, { props: { status, sessionId: ROW.id }, global: { plugins: [router] } });
  return { w, server };
};
const button = (w: Awaited<ReturnType<typeof show>>["w"], name: string) => w.findAll("button, a").find((b) => b.text() === name)!;

afterEach(() => vi.unstubAllGlobals());

test("a waiting turn shows the engine line inline", async () => {
  const { w } = await show(engine({ state: "downloading", detail: "", progress: { done_bytes: 5e9, total_bytes: 10e9, rate_bps: 1e8, eta_s: 120 } }));
  expect(w.get("[role=status]").text()).toBe("Downloading gemma: 5.0 of 10.0 GB, 2 min left");
});

test("waiting for memory offers wait and cancel", async () => {
  const { w, server } = await show(engine({}));
  expect(w.text()).toContain("Waiting for memory: held by reviewer");
  await button(w, "Cancel").trigger("click");
  await flushPromises();
  expect(server.calls.some((c) => c.url.endsWith("/stop") && c.method === "POST")).toBe(true);
  await button(w, "Wait").trigger("click");
  expect(w.find("[role=status]").exists()).toBe(false); // the line closes; the turn keeps waiting
});

test("a failed engine offers retry and open log", async () => {
  const { w } = await show(engine({ state: "failed", detail: "out of memory" }));
  expect(w.get("[role=status]").text()).toBe("gemma failed: out of memory");
  expect(button(w, "Open log").attributes("href")).toBe("/admin");
  expect(button(w, "Retry")).toBeDefined();
});

test("a failed engine's retry resends the person's message", async () => {
  const server = startThread([{ id: "u1", role: "user", parts: [{ type: "text", text: "hi" }], created: 1, usage: null, interrupted: false }]);
  await openChat();
  server.turn = () => new Response(null, { status: 500 });
  const w = mount(WaitingLine, { props: { status: engine({ state: "failed", detail: "boom" }), sessionId: ROW.id }, global: { plugins: [router] } });
  await button(w, "Retry").trigger("click");
  await flushPromises();
  expect(server.calls.some((c) => c.url === "/v1/airlie/chat/turn")).toBe(true);
});
