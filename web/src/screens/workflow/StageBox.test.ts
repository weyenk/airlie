import { mount } from "@vue/test-utils";
import type { FlowNode } from "@/api/workflows";
import { job } from "./fixtures";
import StageBox from "./StageBox.vue";

const node = (over: Partial<FlowNode> = {}): FlowNode => ({
  id: "teams",
  label: "Teams",
  kind: "stage",
  status: "passed",
  jobs: [job({ id: "t1", label: "Team 1", role: null, status: "failed" }), job({ id: "t2", label: "Team 2", role: null, won: true })],
  ...over,
});

test("the box holds its jobs stacked, and only the winner carries the star", () => {
  const w = mount(StageBox, { props: { node: node(), selected: null } });
  expect(w.get("[data-stage]").attributes("data-status")).toBe("passed");
  expect(w.text()).toContain("Teams");
  expect(w.findAll("button[data-job]").map((b) => b.attributes("data-job"))).toEqual(["t1", "t2"]);
  expect(w.findAll("button[data-job]").map((b) => b.text().includes("★ won"))).toEqual([false, true]);
  expect(w.get("[data-stage]").classes()).toContain("flex-col");
});

test("the selected job is marked, and a click says which node and job", async () => {
  const w = mount(StageBox, { props: { node: node(), selected: ["teams", "t2"] } });
  expect(w.findAll("button[data-job]").map((b) => b.attributes("aria-pressed"))).toEqual(["false", "true"]);
  await w.get("[data-job=t1]").trigger("click");
  expect(w.emitted("pick")).toEqual([["teams", "t1"]]);
});

test("a running stage is outlined as running", () => {
  const w = mount(StageBox, { props: { node: node({ status: "running" }), selected: null } });
  expect(w.get("[data-stage]").classes()).toContain("border-info-foreground");
});
