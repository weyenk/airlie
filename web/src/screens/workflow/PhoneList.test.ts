import { mount } from "@vue/test-utils";
import type { FlowCard, FlowNode } from "@/api/workflows";
import { cycle, exitRow, job, lane as laneNode, now, planStage, stage } from "@/test-utils/workflow-fixtures";
import PhoneList from "./PhoneList.vue";

beforeEach(() => {
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: true, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
});
afterEach(() => vi.unstubAllGlobals());

const CARD: FlowCard = {
  title: "Draft all packages",
  tags: [],
  foot: null,
  rows: [
    { kind: "step", status: "passed", text: "L01 · package → critic agree", seconds: 1560 },
    { kind: "step", status: "running", text: "L03 · package · step 6", seconds: 420 },
    { kind: "step", status: "pending", text: "L04 · L05", seconds: null },
  ],
};

const PLAN = () => [
  planStage("intake", "Intake", "passed", {}, "code"),
  planStage("base", "Base", "passed", {}, "code"),
  planStage("gate", "Gate", "passed", {}, "code"),
  planStage("survey", "Survey", "passed", { group: "same-time" }),
  planStage("refine", "Refine", "passed", { group: "same-time" }),
  planStage("layers", "Plan layers"),
  planStage("amigos", "Three amigos"),
  planStage("planned", "Planned", "passed", {}, "milestone"),
];
const BUILD = (): FlowNode[] => {
  const packages: FlowNode = { ...stage("packages", "Draft packages", "running"), lane: "build" };
  packages.jobs[0].card = CARD;
  return [
    packages,
    laneNode("01", "Store", "passed", [cycle("c1", "build 1", "passed")]),
    laneNode("02", "Edit", "pending", []),
    { ...stage("publish", "Publish", "pending", { shape: "code" }), lane: "build" },
  ];
};
const draw = (nodes: FlowNode[], extra: Record<string, unknown> = {}) => mount(PhoneList, { props: { nodes, selected: null, ...extra } });

test("a finished plan lane folds to one row of chips and opens on tap", async () => {
  const w = draw([...PLAN(), ...BUILD()]);
  const folded = w.get("[data-folded=plan]");
  expect(folded.findAll("[data-chip]")).toHaveLength(8);
  expect(w.find("[data-node=intake]").exists()).toBe(false);
  await folded.trigger("click");
  expect(w.find("[data-node=intake]").exists()).toBe(true);
  await w.get("[data-folded=plan]").trigger("click");
  expect(w.find("[data-node=intake]").exists()).toBe(false);
});

test("the lanes sit under PLAN LANE and BUILD LANE labels", () => {
  const w = draw([...PLAN(), ...BUILD()]);
  expect(w.findAll("[data-lane-label]").map((l) => l.text())).toEqual(["PLAN LANE", "BUILD LANE"]);
});

test("the running step is open with its inner steps and the current one outlined", () => {
  const w = draw([...PLAN(), ...BUILD()]);
  const open = w.get("[data-open][data-node=packages]");
  expect(open.findAll("[data-step]")).toHaveLength(3);
  expect(open.findAll("[data-current]")).toHaveLength(1);
  expect(open.get("[data-current]").text()).toContain("L03 · package · step 6");
  expect(w.findAll("[data-open]")).toHaveLength(1);
});

test("a built layer and a pending layer are one row each", () => {
  const w = draw([...PLAN(), ...BUILD()]);
  for (const id of ["layer-01", "layer-02"]) {
    expect(w.findAll(`[data-node=${id}]`)).toHaveLength(1);
  }
  expect(w.get("[data-node=layer-01]").attributes("data-status")).toBe("passed");
  expect(w.get("[data-node=layer-02]").attributes("data-status")).toBe("pending");
});

test("the now bar stays pinned at the top", () => {
  const w = draw(BUILD(), { now: now() });
  expect(w.get("[data-now]").classes()).toContain("sticky");
  expect(w.get("[data-now]").classes().join(" ")).toContain("safe-area-inset-top");
});

test("an 8-layer story with 120-character titles keeps one line per layer", () => {
  const long = "x".repeat(120);
  const nodes = Array.from({ length: 8 }, (_, i) => laneNode(`0${i + 1}`, long, i < 4 ? "passed" : "pending", i < 4 ? [cycle("c1", "build 1", "passed")] : []));
  const w = draw(nodes);
  const rows = w.findAll("[data-node^=layer]");
  expect(rows).toHaveLength(8);
  for (const r of rows) {
    expect(r.classes()).toContain("truncate");
    expect(r.classes()).toContain("min-w-0");
  }
});

test("an open layer folds its earlier tries and lists the live try with its steps", async () => {
  const live = laneNode("02", "Edit", "running", [cycle("c1", "build 1", "failed", "c1-"), { kind: "divider", text: "↩ re-planned", card: null }, cycle("c2", "build 2", "running")]);
  const last = live.rows![2];
  if (last.kind === "cycle") {
    last.jobs[2].card = CARD;
    live.jobs = last.jobs;
  }
  const w = draw([live]);
  const open = w.get("[data-open][data-node=layer-02]");
  const earlier = open.get("[data-earlier]");
  expect(earlier.text()).toContain("build 1 · red → re-planned");
  expect(open.findAll("[data-job]").map((b) => b.attributes("data-job"))).toEqual(["package", "checks", "build"]);
  expect(open.findAll("[data-current]")).toHaveLength(1);
  await earlier.trigger("click");
  expect(w.emitted("pick")).toEqual([["layer-02", "c1-build"]]);
});

test("a tap on a row is passed up with the node and job", async () => {
  const w = draw(BUILD());
  await w.get("[data-node=layer-01]").trigger("click");
  expect(w.emitted("pick")).toEqual([["layer-01", "build"]]);
});

test("the exit row is last", () => {
  const w = draw(BUILD(), { exit: exitRow() });
  const kids = [...w.element.children];
  expect(kids[kids.length - 1].hasAttribute("data-exit-row")).toBe(true);
});

test("a failed job opens its node when nothing runs", () => {
  const nodes = [{ ...stage("packages", "Draft packages", "failed"), lane: "build" as const }, laneNode("01", "Store", "pending", [])];
  nodes[0].jobs[0] = job({ id: "packages", label: "Draft packages", status: "failed" });
  expect(draw(nodes).find("[data-open][data-node=packages]").exists()).toBe(true);
});
