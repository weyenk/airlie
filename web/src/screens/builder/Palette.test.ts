import { mount } from "@vue/test-utils";
import Palette from "./Palette.vue";

test("the node types in their shapes, then the bundles by name", () => {
  const w = mount(Palette, { props: { bundles: ["layer", "fix"] } });
  const types = w.findAll("[data-palette-type]");
  // schema v2: the four kinds; a bundle box comes from the bundles below
  expect(types.map((t) => t.attributes("data-palette-type"))).toEqual([
    "WORK",
    "ROUND_ROBIN",
    "SUPERVISOR_WORKERS",
    "K_INDEPENDENT_TEAMS",
  ]);
  expect(w.get("[data-palette-type=WORK]").attributes("data-shape")).toBe("work");
  expect(w.get("[data-palette-type=K_INDEPENDENT_TEAMS]").attributes("data-shape")).toBe("work");
  expect(w.get("[data-palette-type=ROUND_ROBIN]").attributes("data-shape")).toBe("work");
  expect(w.findAll("[data-bundle]").map((b) => b.text())).toEqual(["layer", "fix"]);
});

test("no bundles says so", () => {
  expect(mount(Palette, { props: { bundles: [] } }).get("[data-bundles]").text()).toContain("None yet");
});

test("a type or a bundle drags out with what it places", async () => {
  const w = mount(Palette, { props: { bundles: ["layer"] } });
  const set: [string, string][] = [];
  const dataTransfer = { setData: (k: string, v: string) => set.push([k, v]), effectAllowed: "" };
  await w.get("[data-palette-type=WORK]").trigger("dragstart", { dataTransfer });
  await w.get("[data-bundle]").trigger("dragstart", { dataTransfer });
  expect(set).toEqual([
    ["application/x-airlie-node", "WORK"],
    ["application/x-airlie-bundle", "layer"],
  ]);
  expect(w.get("[data-palette-type=WORK]").attributes("draggable")).toBe("true");
});

test("a click (or a tap, where nothing drags) adds one too", async () => {
  const w = mount(Palette, { props: { bundles: ["layer"] } });
  await w.get("[data-palette-type=WORK]").trigger("click");
  await w.get("[data-bundle]").trigger("click");
  expect(w.emitted("add")).toEqual([[{ kind: "WORK" }], [{ bundle: "layer" }]]);
});

test("read-only, nothing drags or adds", async () => {
  const w = mount(Palette, { props: { bundles: ["layer"], readonly: true } });
  expect(w.get("[data-palette-type=WORK]").attributes("draggable")).toBe("false");
  await w.get("[data-palette-type=WORK]").trigger("click");
  expect(w.emitted("add")).toBeUndefined();
});
