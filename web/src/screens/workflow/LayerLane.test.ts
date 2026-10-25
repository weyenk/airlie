import { mount } from "@vue/test-utils";
import { layer } from "./fixtures";
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
