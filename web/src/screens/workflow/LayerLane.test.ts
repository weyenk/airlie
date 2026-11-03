import { mount } from "@vue/test-utils";
import { cycle, lane as laneNode, layer } from "@/test-utils/workflow-fixtures";
import LayerLane from "./LayerLane.vue";

const lane = (node = layer("01", "Comment id store", "passed", ["passed", "passed", "passed"]), selected: [string, string] | null = null) =>
  mount(LayerLane, { props: { node, selected } });

test("a lane has the layer's head, its acceptance ids and its three jobs", () => {
  const w = lane(layer("03", "Card modal", "running", ["passed", "passed", "running"], { acceptance: ["AC3", "AC4"] }));
  expect(w.attributes("data-node")).toBe("layer-03");
  expect(w.attributes("data-status")).toBe("running");
  expect(w.text()).toContain("Layer 03 · Card modal");
  expect(w.get("[data-acceptance]").text()).toBe("AC3 AC4");
  expect(w.findAll("button").map((b) => b.attributes("data-job"))).toEqual(["tests", "checks", "build"]);
});

test("a 120-character title truncates in its lane, with the full text in its title", () => {
  const long = "x".repeat(120);
  const w = lane(layer("01", long, "passed", ["passed", "passed", "passed"]));
  const head = w.get("[data-title]");
  expect(head.classes()).toContain("truncate");
  expect(head.attributes("title")).toContain(long);
  expect(w.get("[data-acceptance]").classes()).toContain("whitespace-nowrap");
});

test("the selected job is pressed and a pick is passed up", async () => {
  const w = lane(undefined, ["layer-01", "tests"]);
  expect(w.get("[data-job=tests]").attributes("aria-pressed")).toBe("true");
  expect(w.get("[data-job=build]").attributes("aria-pressed")).toBe("false");
  await w.get("[data-job=build]").trigger("click");
  expect(w.emitted("pick")).toEqual([["layer-01", "build"]]);
});

test("a layer that has not started is only its header", () => {
  const w = mount(LayerLane, { props: { node: laneNode("04", "State bench", "pending", [], { tags: [{ text: "drafting", tone: "chip" }] }), selected: null } });
  expect(w.text()).toContain("State bench");
  expect(w.text()).toContain("drafting");
  expect(w.find("[data-job]").exists()).toBe(false);
  const pend = mount(LayerLane, { props: { node: laneNode("05", "Other", "pending", [cycle("c1", "build 1", "pending")]), selected: null } });
  expect(pend.find("[data-job]").exists()).toBe(false);
});

test("a resumed layer shows each try cycle and a divider between", async () => {
  const rows = [cycle("c1", "build 1", "failed", "c1-"), { kind: "divider" as const, text: "↩ re-planned", card: null }, cycle("c2", "build 2", "running")];
  const w = mount(LayerLane, { props: { node: laneNode("02", "Edit", "running", rows), selected: null } });
  expect(w.findAll("[data-row]")).toHaveLength(2);
  expect(w.findAll("[data-divider]")).toHaveLength(1);
  expect(w.get("[data-divider]").text()).toContain("re-planned");
  expect(w.text()).toContain("build 1");
  await w.get("[data-job=c1-build]").trigger("click");
  expect(w.emitted("pick")).toEqual([["layer-02", "c1-build"]]);
});

test("one cycle has no label prefix", () => {
  const w = mount(LayerLane, { props: { node: laneNode("02", "Edit", "running", [cycle("c1", "build 1", "running")]), selected: null } });
  expect(w.text()).not.toContain("build 1");
});
