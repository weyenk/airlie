import { mount } from "@vue/test-utils";
import Palette from "./Palette.vue";

test("the node types in their shapes, then the bundles by name", () => {
  const w = mount(Palette, { props: { bundles: ["layer", "fix"] } });
  const types = w.findAll("[data-palette-type]");
  // schema v2: no parallel or each box; a bundle box comes from the bundles below
  expect(types.map((t) => t.attributes("data-palette-type"))).toEqual(["ask", "conversation", "write", "meeting", "team", "check", "gate"]);
  expect(w.get("[data-palette-type=check]").attributes("data-shape")).toBe("code");
  expect(w.get("[data-palette-type=gate]").attributes("data-shape")).toBe("gate");
  expect(w.get("[data-palette-type=ask]").attributes("data-shape")).toBe("model");
  expect(w.findAll("[data-bundle]").map((b) => b.text())).toEqual(["layer", "fix"]);
});

test("no bundles says so", () => {
  expect(mount(Palette, { props: { bundles: [] } }).get("[data-bundles]").text()).toContain("None yet");
});

test("a type or a bundle drags out with what it places", async () => {
  const w = mount(Palette, { props: { bundles: ["layer"] } });
  const set: [string, string][] = [];
  const dataTransfer = { setData: (k: string, v: string) => set.push([k, v]), effectAllowed: "" };
  await w.get("[data-palette-type=check]").trigger("dragstart", { dataTransfer });
  await w.get("[data-bundle]").trigger("dragstart", { dataTransfer });
  expect(set).toEqual([
    ["application/x-airlie-node", "check"],
    ["application/x-airlie-bundle", "layer"],
  ]);
  expect(w.get("[data-palette-type=check]").attributes("draggable")).toBe("true");
});

test("a click (or a tap, where nothing drags) adds one too", async () => {
  const w = mount(Palette, { props: { bundles: ["layer"] } });
  await w.get("[data-palette-type=gate]").trigger("click");
  await w.get("[data-bundle]").trigger("click");
  expect(w.emitted("add")).toEqual([[{ type: "gate" }], [{ bundle: "layer" }]]);
});

test("read-only, nothing drags or adds", async () => {
  const w = mount(Palette, { props: { bundles: ["layer"], readonly: true } });
  expect(w.get("[data-palette-type=check]").attributes("draggable")).toBe("false");
  await w.get("[data-palette-type=gate]").trigger("click");
  expect(w.emitted("add")).toBeUndefined();
});
