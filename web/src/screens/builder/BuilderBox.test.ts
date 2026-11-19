import { mount } from "@vue/test-utils";
import BuilderBox from "./BuilderBox.vue";
import type { GNode } from "./graph";

const node = (over: Partial<GNode> = {}): GNode => ({ id: "gate", kind: "WORK", spec: { run: "story.gate" }, x: 0, y: 0, ...over });

test("a click picks the box and a double-click opens it", async () => {
  const w = mount(BuilderBox, { props: { node: node() } });
  await w.trigger("click");
  await w.trigger("dblclick");
  expect(w.emitted("pick")).toEqual([["gate"]]);
  expect(w.emitted("open")).toEqual([["gate"]]);
});

test("a conversation box says what it agrees on or how many it runs", () => {
  const rr = mount(BuilderBox, { props: { node: node({ id: "agree", kind: "ROUND_ROBIN", spec: { agreement_when: "all approved" } }) } });
  expect(rr.text()).toContain("all approved");
  const sw = mount(BuilderBox, { props: { node: node({ id: "panel", kind: "SUPERVISOR_WORKERS", spec: { workers: 3 } }) } });
  expect(sw.text()).toContain("3 workers");
});
