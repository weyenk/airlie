import { mount } from "@vue/test-utils";
import { job, layer, stage } from "@/test-utils/workflow-fixtures";
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

test("on a phone the pipeline runs top to bottom, each layer's three jobs side by side", async () => {
  phone(true);
  const w = draw();
  expect(w.find("[data-flow]").exists()).toBe(false);
  const rows = w.findAll("[data-vlane]");
  expect(rows).toHaveLength(2);
  expect(rows[0].findAll("[data-job]").map((b) => b.text())).toEqual(["Tests", "Checks", "Build"]);
  expect(rows[0].get("[data-jobs]").classes()).toContain("flex");
  expect(w.findAll("[data-vstage]").map((s) => s.attributes("data-job"))).toEqual(["intake", "refine", "layers", "publish", "review"]);
  expect(rows[1].get("[data-job=checks]").text()).toBe("Checks 2x");
  await rows[1].get("[data-job=build]").trigger("click");
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

test("on a phone the meeting gets its own row under Plan layers", async () => {
  phone(true);
  const w = draw(WITH_MEETING());
  const rows = w.findAll("[data-vstage]");
  expect(rows.map((r) => [r.attributes("data-node"), r.attributes("data-job")])).toEqual([
    ["intake", "intake"],
    ["refine", "refine"],
    ["layers", "layers"],
    ["layers", "meeting"],
    ["publish", "publish"],
  ]);
  expect(rows[2].text()).toContain("Plan layers");
  expect(rows[3].text()).toContain("Three amigos");
  await rows[3].trigger("click");
  expect(w.emitted("pick")).toEqual([["layers", "meeting"]]);
});
