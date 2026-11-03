import { mount } from "@vue/test-utils";
import { cycle, exitRow, job, lane as laneNode, layer, now, planStage, stage } from "@/test-utils/workflow-fixtures";
import Pipeline from "./Pipeline.vue";

const STAGES_ONLY = [stage("intake", "Intake"), stage("refine", "Refine", "running"), stage("survey", "Survey", "skipped"), stage("layers", "Plan layers", "pending"), stage("publish", "Publish", "pending"), stage("review", "Review", "pending")];
const WITH_LAYERS = [
  stage("intake", "Intake"),
  stage("refine", "Refine"),
  stage("layers", "Plan layers"),
  layer("01", "Comment id store", "passed", ["passed", "passed", "passed"]),
  layer("02", "Comment edit", "running", ["passed", "passed", "running"]),
  stage("publish", "Publish", "pending"),
  stage("review", "Review", "pending"),
];
WITH_LAYERS[4].jobs[1].attempts = 2; // layer 02's checks ran twice

function phone(on: boolean) {
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: on, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
}
beforeEach(() => phone(false));
afterEach(() => vi.unstubAllGlobals());

const draw = (nodes = WITH_LAYERS, extra: Record<string, unknown> = {}) => mount(Pipeline, { props: { nodes, selected: null, progress: null, ...extra } });

test("a story with no layers yet draws only its stages, and not a stage that never ran", () => {
  const w = draw(STAGES_ONLY);
  expect(w.findAll("[data-lane]")).toHaveLength(0);
  expect(w.findAll("button").map((b) => b.attributes("data-job"))).toEqual(["intake", "refine", "layers", "publish", "review"]);
  expect(w.text()).not.toContain("Survey");
});

test("stages run left to right with a lane per layer between Plan layers and Publish", () => {
  const w = draw();
  expect(w.findAll("[data-lane]").map((l) => l.attributes("data-node"))).toEqual(["layer-01", "layer-02"]);
  const order = w.findAll("button").map((b) => b.attributes("data-job"));
  expect(order).toEqual(["intake", "refine", "layers", "tests", "checks", "build", "tests", "checks", "build", "publish", "review"]);
});

test("the flow scrolls sideways with a fade", () => {
  const flow = draw().get("[data-flow]");
  expect(flow.classes()).toContain("overflow-x-auto");
  expect(flow.classes().join(" ")).toContain("mask-image");
});

test("the progress figure shows once there are layers", () => {
  expect(draw(WITH_LAYERS, { progress: 62 }).get("[data-progress]").text()).toBe("62% · 1 of 2 layers built");
  expect(draw(WITH_LAYERS).get("[data-progress]").text()).toBe("50% · 1 of 2 layers built");
  expect(draw(STAGES_ONLY).find("[data-progress]").exists()).toBe(false);
});

test("a pick from a box is passed up", async () => {
  const w = draw();
  await w.get("[data-lane][data-node=layer-02] [data-job=build]").trigger("click");
  expect(w.emitted("pick")).toEqual([["layer-02", "build"]]);
});

test("on a phone the pipeline is the folding list: no flow row, a built layer is one row, the running layer opens", async () => {
  phone(true);
  const w = draw(WITH_LAYERS, { now: now() });
  expect(w.find("[data-flow]").exists()).toBe(false);
  expect(w.find("[data-phone-list]").exists()).toBe(true);
  expect(w.get("[data-now]").classes()).toContain("sticky");
  expect(w.find("button[data-node=layer-01]").exists()).toBe(true);
  await w.get("[data-open][data-node=layer-02] [data-job=build]").trigger("click");
  expect(w.emitted("pick")).toEqual([["layer-02", "build"]]);
});

// the story meeting is the layers stage's second job (the server's `_meeting_job`)
const WITH_MEETING = () => {
  const layers = stage("layers", "Plan layers");
  layers.jobs.push(job({ id: "meeting", label: "Three amigos", role: "amigos", status: "running", duration_s: 240 }));
  return [stage("intake", "Intake"), stage("refine", "Refine"), layers, stage("publish", "Publish", "pending")];
};

test("a stage with more than one job stacks them: the three amigos under Plan layers", async () => {
  const w = draw(WITH_MEETING());
  const layers = w.get("[data-stage][data-node=layers]");
  expect(layers.findAll("[data-job]").map((b) => b.attributes("data-job"))).toEqual(["layers", "meeting"]);
  const box = layers.get("[data-job=meeting]");
  expect(box.text()).toContain("Three amigos");
  expect(box.text()).toContain("amigos · 4 min");
  expect(box.attributes("data-status")).toBe("running");
  await box.trigger("click");
  expect(w.emitted("pick")).toEqual([["layers", "meeting"]]);
});

test("the meeting is picked out when selected", () => {
  const w = draw(WITH_MEETING(), { selected: ["layers", "meeting"] });
  expect(w.get("[data-job=meeting]").attributes("aria-pressed")).toBe("true");
  expect(w.get("[data-job=layers]").attributes("aria-pressed")).toBe("false");
});

const FULL = () => [
  planStage("intake", "Intake", "passed", {}, "code"),
  planStage("base", "Base", "passed", {}, "code"),
  planStage("gate", "Gate", "passed", {}, "code"),
  planStage("survey", "Survey", "passed", { group: "same-time" }),
  planStage("refine", "Refine", "passed", { group: "same-time" }),
  planStage("layers", "Plan layers"),
  planStage("amigos", "Three amigos"),
  planStage("planned", "Planned", "passed", {}, "milestone"),
  planStage("packages", "Draft packages", "running"),
  laneNode("01", "Store", "passed", [cycle("c1", "build 1", "passed")]),
  laneNode("02", "Edit", "running", [cycle("c1", "build 1", "failed", "c1-"), { kind: "divider", text: "↩ re-planned", card: null }, cycle("c2", "build 2", "running")]),
  laneNode("03", "Later", "pending", []),
  stage("publish", "Publish", "pending", { shape: "code" }),
  stage("review", "Review", "pending", { shape: "code" }),
];

test("the row runs intake, base, gate, the same-time pair, plan layers, amigos, planned, drafting, the lanes, publish, review", () => {
  const w = draw(FULL());
  const top = w.get("[data-flow]").findAll(":scope > [data-stage], :scope > [data-group], :scope > [data-lanes] > [data-lane]");
  expect(top.map((t) => t.attributes("data-node") ?? "group")).toEqual(["intake", "base", "gate", "group", "layers", "amigos", "planned", "packages", "layer-01", "layer-02", "layer-03", "publish", "review"]);
  const group = w.get("[data-group]");
  expect(group.text()).toContain("same time");
  expect(group.findAll("[data-stage]").map((s) => s.attributes("data-node"))).toEqual(["survey", "refine"]);
});

test("boxes draw by shape: a milestone diamond, code boxes, model boxes", () => {
  const w = draw(FULL());
  expect(w.find("[data-node=planned] [data-shape=milestone]").exists()).toBe(true);
  expect(w.find("[data-node=intake] [data-shape=code]").exists()).toBe(true);
  expect(w.find("[data-node=layers] [data-shape=model]").exists()).toBe(true);
});

test("a stage with no jobs is not drawn", () => {
  const nodes = FULL();
  nodes[3] = { ...nodes[3], jobs: [] };
  const w = draw(nodes);
  expect(w.find("[data-node=survey]").exists()).toBe(false);
  // refine alone needs no bracket
  expect(w.find("[data-group]").exists()).toBe(false);
  expect(w.find("[data-node=refine]").exists()).toBe(true);
});

test("the now bar and the exit row sit at the top and the end", async () => {
  const w = draw(FULL(), { now: now(), exit: exitRow() });
  expect(w.get("[data-now]").text()).toContain("Build lane › Draft packages");
  expect(w.get("[data-exit=blocked]").text()).toContain("blocked 3 times");
  await w.get("[data-exit] [data-job=diagnose]").trigger("click");
  expect(w.emitted("pick")).toEqual([["exit", "diagnose"]]);
  expect(draw(FULL()).find("[data-now]").exists()).toBe(false);
  expect(draw(FULL()).find("[data-exit]").exists()).toBe(false);
});

test("a not-started layer is its header only; a resumed one shows both builds and a divider", () => {
  const w = draw(FULL());
  expect(w.find("[data-lane][data-node=layer-03] [data-job]").exists()).toBe(false);
  expect(w.findAll("[data-lane][data-node=layer-02] [data-divider]")).toHaveLength(1);
  expect(w.findAll("[data-lane][data-node=layer-02] [data-job=build], [data-lane][data-node=layer-02] [data-job=c1-build]")).toHaveLength(2);
});

test("on a phone the meeting gets its own row under Plan layers", async () => {
  phone(true);
  const w = draw(WITH_MEETING());
  const open = w.get("[data-open][data-node=layers]");
  expect(open.findAll("[data-job]").map((r) => r.attributes("data-job"))).toEqual(["layers", "meeting"]);
  expect(open.get("[data-job=meeting]").text()).toContain("Three amigos");
  await open.get("[data-job=meeting]").trigger("click");
  expect(w.emitted("pick")).toEqual([["layers", "meeting"]]);
});
