import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { engine, STATUS, statusServer, vmRow } from "@/test-utils/chat-status";
import { stubWindowWidth, thread } from "@/test-utils/chat-panel";
import { message, openChat, ROW } from "@/test-utils/thread";
import SessionStatus from "./SessionStatus.vue";

const NOW = new Date(2026, 10, 12, 15, 0, 0);
const mounted: ReturnType<typeof mount>[] = [];

async function show(session = ROW) {
  const w = mount(SessionStatus, { props: { session }, attachTo: document.body });
  mounted.push(w);
  await flushPromises();
  return w;
}
const chip = () => document.body.querySelector<HTMLElement>("[data-status-chip]")!;
const opened = async () => {
  chip().click();
  await flushPromises();
  return document.body.querySelector<HTMLElement>("[data-status-detail]");
};

beforeEach(() => {
  setActivePinia(createPinia());
  stubWindowWidth(1280);
  vi.useFakeTimers({ now: NOW, toFake: ["Date"] });
});
afterEach(() => {
  for (const w of mounted.splice(0)) w.unmount();
  document.body.innerHTML = "";
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("ready shows only the dot", async () => {
  statusServer({ engines: [engine(), vmRow()] });
  const w = await show();
  expect(w.text()).toBe("");
  expect(chip().getAttribute("aria-label")).toBe("Status: ready");
  expect(w.find('[role="status"]').exists()).toBe(false);
});

test.each([
  [{ state: "starting" as const }, "Starting gemma…"],
  [{ state: "downloading" as const, progress: { done_bytes: 5e9, total_bytes: 10e9, rate_bps: 1e8, eta_s: 120 } }, "Downloading gemma: 5.0 of 10.0 GB, 2 min left"],
  [{ state: "waiting" as const, detail: "waiting for memory: held by reviewer" }, "Waiting for memory: held by reviewer"],
])("a %# engine that is not ready shows the engine line", async (over, line) => {
  statusServer({ engines: [engine(over), vmRow()] });
  const w = await show();
  expect(w.get('[role="status"]').text()).toBe(line);
});

test("a starting VM says starting a VM for the repository and its step", async () => {
  statusServer({ engines: [engine(), vmRow({ state: "starting", detail: "building image" })] });
  const w = await show();
  expect(w.get('[role="status"]').text()).toBe("Starting a VM for acme/api…");
  expect(w.text()).toContain("building image");
});

test("a failed engine shows its reason", async () => {
  statusServer({ engines: [engine({ state: "failed", detail: "out of memory" }), vmRow()] });
  const w = await show();
  expect(w.get('[role="status"]').text()).toBe("gemma failed: out of memory");
});

test("a failed engine is said before a VM that is only starting", async () => {
  statusServer({ engines: [engine({ state: "failed", detail: "out of memory" }), vmRow({ state: "starting" })] });
  const w = await show();
  expect(w.get('[role="status"]').text()).toBe("gemma failed: out of memory");
});

test("the chip opens the popover with vm, engine, context, memory and branch", async () => {
  const server = statusServer({
    engines: [engine(), vmRow()],
    status: { ...STATUS, vm: { ...STATUS.vm!, since: NOW.getTime() / 1000 - 14 * 60 } },
    messages: [message("m1", "assistant", [{ type: "text", text: "hi" }])],
  });
  server.messages[0].usage = { prompt_tokens: 100_000, completion_tokens: 18_000, cached_tokens: 0, max_context: 262_144 };
  await show();
  await openChat();
  const pop = await opened();
  expect(pop).not.toBeNull();
  const text = pop!.textContent!;
  expect(text).toContain("VM ready");
  expect(text).toContain("vm:chat-1, up 14 min");
  expect(text).toContain("gemma loaded");
  expect(text).toContain("Gemma 4, 29.6 GB");
  expect(text).not.toContain("gemma4"); // the model's name, not its key
  expect(text).toContain("118k of 262k tokens");
  expect(text).toContain("41 of 64 GB in use");
  expect(text).toContain("airlie/chat-abc");
  expect(text).toContain("3 commits ahead of main, not pushed");
  expect(server.calls.filter((u) => u.endsWith("/status")).length).toBeGreaterThan(0);
});

test("the popover says what a waiting turn waits for in place of the memory figure", async () => {
  statusServer({
    engines: [engine({ state: "waiting", detail: "waiting for memory: held by reviewer" }), vmRow()],
    status: { ...STATUS, memory: { used_gb: 60, budget_gb: 64, waiting: "waiting for memory: held by reviewer" } },
  });
  await show();
  const pop = await opened();
  expect(pop!.textContent).toContain("Waiting for memory: held by reviewer");
  expect(pop!.textContent).not.toContain("in use");
});

test("a starting VM shows its step in the popover in place of its uptime", async () => {
  statusServer({
    engines: [engine(), vmRow({ state: "starting", detail: "cloning" })],
    status: { ...STATUS, vm: { ...STATUS.vm!, state: "starting", detail: "cloning" } },
  });
  await show();
  const pop = await opened();
  expect(pop!.textContent).toContain("cloning");
  expect(pop!.textContent).not.toContain("up ");
});

test("a failed status load keeps the chip and says the detail is unavailable", async () => {
  statusServer({ engines: [engine(), vmRow()], down: true });
  await show();
  const pop = await opened();
  expect(chip()).not.toBeNull();
  expect(pop!.textContent).toContain("Status detail is unavailable.");
});

test("a plain chat's popover has no vm or branch row", async () => {
  statusServer({ status: { ...STATUS, vm: null, branch: null } });
  await show(thread("c1"));
  const pop = await opened();
  expect(pop!.textContent).toContain("gemma loaded");
  expect(pop!.textContent).not.toContain("VM");
  expect(pop!.textContent).not.toContain("ahead of");
  expect(pop!.querySelector("[data-row=branch]")).toBeNull();
  expect(pop!.querySelector("[data-row=vm]")).toBeNull();
});

test("on a phone the chip opens a bottom sheet and shows only the dot", async () => {
  stubWindowWidth(500);
  statusServer({ engines: [engine({ state: "starting" }), vmRow()] });
  const w = await show();
  expect(w.find('[role="status"]').exists()).toBe(false); // the dot alone; the words are in the sheet
  expect(chip().getAttribute("aria-label")).toBe("Status: starting gemma");
  await opened();
  const sheet = document.body.querySelector("[role=dialog]");
  expect(sheet?.textContent).toContain("Starting gemma…");
  expect(sheet?.textContent).toContain("acme/api");
  expect(sheet?.textContent).toContain("airlie/chat-abc");
});
