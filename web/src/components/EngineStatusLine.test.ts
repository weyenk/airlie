import { mount } from "@vue/test-utils";
import type { EngineStatus } from "@/api/engines";
import EngineStatusLine from "./EngineStatusLine.vue";

const engine = (over: Partial<EngineStatus>): EngineStatus => ({
  name: "reviewer",
  kind: "engine",
  state: "ready",
  detail: "",
  progress: null,
  holders: [],
  lifecycle: "on_demand",
  roles: ["reviewer"],
  ...over,
});
const show = (over: Partial<EngineStatus>) => mount(EngineStatusLine, { props: { status: engine(over) } });

test("a label is said in place of the resource name", () => {
  const w = mount(EngineStatusLine, { props: { status: engine({ name: "vm:chat-1", state: "starting" }), label: "a VM for acme/api" } });

  expect(w.text()).toBe("Starting a VM for acme/api…");
});

test("a download says how much has arrived and how long is left, with a progress bar", () => {
  const w = show({
    state: "downloading",
    detail: "org/reviewer",
    progress: { done_bytes: 7.2e9, total_bytes: 19.4e9, rate_bps: 1e7, eta_s: 180 },
  });

  expect(w.text()).toBe("Downloading reviewer: 7.2 of 19.4 GB, 3 min left");
  const bar = w.get('[role="progressbar"]');
  expect(Number(bar.attributes("aria-valuenow"))).toBeCloseTo(37.1, 0);
});

test("a download with no estimate yet says no time left", () => {
  const w = show({ state: "downloading", progress: { done_bytes: 1e9, total_bytes: 4e9, rate_bps: 0, eta_s: null } });

  expect(w.text()).toBe("Downloading reviewer: 1.0 of 4.0 GB");
});

test("a download that has not reported a size is a bar with no number", () => {
  const w = show({ state: "downloading", progress: null });

  expect(w.text()).toBe("Downloading reviewer");
  expect(w.get('[role="progressbar"]').attributes("aria-valuenow")).toBeUndefined();
});

test("waiting for memory names who holds it", () => {
  const w = show({ state: "waiting", detail: "waiting for memory: chat:1, role:reviewer" });

  expect(w.text()).toBe("Waiting for memory: chat:1, role:reviewer");
  expect(w.find('[role="progressbar"]').exists()).toBe(false);
});

test("a failed engine says why", () => {
  const w = show({ state: "failed", detail: "port 8081 is taken" });

  expect(w.text()).toBe("reviewer failed: port 8081 is taken");
});

test.each([
  ["starting", "Starting reviewer…"],
  ["stopping", "Stopping reviewer"],
] as const)("an engine that is %s says so", (state, text) => {
  expect(show({ state }).text()).toBe(text);
});

test.each(["ready", "stopped"] as const)("a %s engine shows nothing", (state) => {
  const w = show({ state });

  expect(w.text()).toBe("");
  expect(w.find('[role="status"]').exists()).toBe(false);
});
