import { mount } from "@vue/test-utils";
import Palette from "./Palette.vue";

test("the node types in their shapes, then the bundles by name", () => {
  const w = mount(Palette, { props: { bundles: ["layer", "fix"] } });
  const types = w.findAll("[data-palette-type]");
  expect(types.map((t) => t.attributes("data-palette-type"))).toEqual(["ask", "conversation", "write", "meeting", "check", "gate", "parallel", "each"]);
  expect(w.get("[data-palette-type=check]").attributes("data-shape")).toBe("code");
  expect(w.get("[data-palette-type=gate]").attributes("data-shape")).toBe("gate");
  expect(w.get("[data-palette-type=ask]").attributes("data-shape")).toBe("model");
  expect(w.findAll("[data-bundle]").map((b) => b.text())).toEqual(["layer", "fix"]);
});

test("no bundles says so", () => {
  expect(mount(Palette, { props: { bundles: [] } }).get("[data-bundles]").text()).toContain("None yet");
});
