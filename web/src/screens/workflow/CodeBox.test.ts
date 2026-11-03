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

test("its second line is the server's sub: a ref and sha, a test file count, a fix card's key", () => {
  expect(box({ sub: "origin/main e8146e0" }).text()).toContain("origin/main e8146e0");
  expect(box({ sub: "AIR-48", tags: [{ text: "later", tone: "warn" }] }).text()).not.toContain("later");
});

test("a running box's live step wins over its sub, and tags show when there is no sub", () => {
  const running = box({ status: "running", live: "try 2", sub: "1 test file" });
  expect(running.text()).toContain("try 2");
  expect(running.text()).not.toContain("1 test file");
  expect(box({ tags: [{ text: "2 of 2", tone: "warn" }] }).text()).toContain("2 of 2");
});

test("a click picks the job", async () => {
  const w = box();
  await w.trigger("click");
  expect(w.emitted("pick")).toEqual([["layer-01", "gate"]]);
});
