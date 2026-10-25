import { mount } from "@vue/test-utils";
import { job } from "./fixtures";
import JobBox from "./JobBox.vue";

const box = (props: Record<string, unknown>) => mount(JobBox, { props: { nodeId: "layer-01", job: job(), ...props } });

test("a box shows its label, its role and how long it took", () => {
  const w = box({});
  expect(w.text()).toContain("Build");
  expect(w.text()).toContain("coder · 2 min");
});

test("a job that has not run says its status", () => {
  expect(box({ job: job({ status: "pending", role: null, duration_s: null }) }).text()).toContain("pending");
});

test("more than one attempt shows an Nx badge", () => {
  expect(box({ job: job({ attempts: 2 }) }).text()).toContain("2x");
  expect(box({}).text()).not.toContain("1x");
});

test("only a running box pulses", () => {
  const running = box({ job: job({ status: "running" }) });
  expect(running.attributes("data-status")).toBe("running");
  expect(running.classes().join(" ")).toContain("motion-safe:animate-pulse");
  expect(box({}).classes().join(" ")).not.toContain("animate-pulse");
});

test("a click picks the job, and the selected box says so", async () => {
  const w = box({ selected: true });
  expect(w.attributes("aria-pressed")).toBe("true");
  await w.trigger("click");
  expect(w.emitted("pick")).toEqual([["layer-01", "build"]]);
});
