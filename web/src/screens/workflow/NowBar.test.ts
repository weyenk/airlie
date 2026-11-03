import { mount } from "@vue/test-utils";
import { now } from "@/test-utils/workflow-fixtures";
import NowBar from "./NowBar.vue";

const sec = () => Date.now() / 1000;

test("a running story names its step path, inner step, step count and last event", () => {
  const w = mount(NowBar, { props: { now: now({ started: sec() - 125, last_event: sec() - 4 }) } });
  const bar = w.get("[data-now]");
  expect(bar.attributes("data-now-status")).toBe("running");
  expect(bar.text()).toContain("Running");
  expect(bar.text()).toContain("Build lane › Draft packages");
  expect(bar.text()).toContain("L03 · package");
  expect(bar.text()).toContain("step 6 · 2 min");
  expect(bar.text()).toMatch(/last event \d+ s ago/);
});

test("quiet time changes no color", () => {
  const w = mount(NowBar, { props: { now: now({ started: sec() - 900, last_event: sec() - 800 }) } });
  expect(w.get("[data-now]").html()).not.toMatch(/warn|danger/);
});

test("a stopped or idle story shows its text", () => {
  const w = mount(NowBar, { props: { now: now({ status: "stopped", text: "Stopped at L02 build" }) } });
  expect(w.get("[data-now]").attributes("data-now-status")).toBe("stopped");
  expect(w.text()).toContain("Stopped at L02 build");
  expect(w.text()).not.toContain("Running");
});
