import { mount } from "@vue/test-utils";
import BuilderBox from "./BuilderBox.vue";
import type { GNode } from "./graph";

const node = (over: Partial<GNode> = {}): GNode => ({ id: "gate", type: "gate", spec: { run: "story.gate" }, x: 0, y: 0, ...over });

test("a click picks the box and a double-click opens it", async () => {
  const w = mount(BuilderBox, { props: { node: node() } });
  await w.trigger("click");
  await w.trigger("dblclick");
  expect(w.emitted("pick")).toEqual([["gate"]]);
  expect(w.emitted("open")).toEqual([["gate"]]);
});

test("a bundle box names the workflow it runs and what it runs for", () => {
  const w = mount(BuilderBox, { props: { node: node({ id: "build", type: "bundle", spec: { workflow: "layer", each: "layers" } }) } });
  expect(w.attributes("data-shape")).toBe("bundle");
  expect(w.get("[data-bundle-name]").text()).toBe("layer");
  expect(w.text()).toContain("for each layers");
});
