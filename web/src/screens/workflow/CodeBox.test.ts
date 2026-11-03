import { mount } from "@vue/test-utils";
import { job } from "@/test-utils/workflow-fixtures";
import CodeBox from "./CodeBox.vue";
import { BOX } from "./style";

const box = (over = {}, props: Record<string, unknown> = {}) =>
  mount(CodeBox, { props: { nodeId: "layer-01", job: job({ id: "gate", label: "gate", role: null, shape: "code", ...over }), ...props } });

test("a code step draws square with a mono label and its status", () => {
  const w = box();
  expect(w.attributes("data-shape")).toBe("code");
  expect(w.attributes("data-status")).toBe("passed");
  expect(w.text()).toContain("gate");
  const cls = w.classes().join(" ");
  expect(cls).toContain("font-mono");
  expect(cls).toContain("rounded-[4px]");
});

test("a stopped box is dashed amber", () => {
  const w = box({ status: "stopped" });
  expect(w.attributes("data-status")).toBe("stopped");
  for (const c of BOX.stopped.split(" ")) expect(w.classes()).toContain(c);
});

test("a click picks the job", async () => {
  const w = box();
  await w.trigger("click");
  expect(w.emitted("pick")).toEqual([["layer-01", "gate"]]);
});
