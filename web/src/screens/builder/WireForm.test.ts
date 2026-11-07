import { mount } from "@vue/test-utils";
import type { GNode } from "./graph";
import WireForm from "./WireForm.vue";

const from: GNode = { id: "check_it", type: "check", spec: { run: "x.c", outputs: { verdict: "text", notes: "markdown" } }, x: 0, y: 0 };
const to: GNode = { id: "build", type: "ask", spec: { role: "coder", prompt: "x.b" }, x: 160, y: 0 };
const each: GNode = { id: "each-1", type: "each", spec: { over: "layers", workflow: "" }, x: 320, y: 0 };
const mountForm = (props: Record<string, unknown> = {}) => mount(WireForm, { props: { from, to, bundles: ["layer"], predicates: ["x.again"], ...props } });

test("five moves; sequence applies as it is", async () => {
  const w = mountForm();
  expect(w.findAll("input[type=radio]").map((r) => r.attributes("value"))).toEqual(["sequence", "parallel", "loop", "branch", "each"]);
  expect(w.text()).toContain("check_it");
  expect(w.text()).toContain("build");
  await w.get("[data-wire-apply]").trigger("click");
  expect(w.emitted("apply")).toEqual([["sequence", {}]]);
});

test("a loop takes its max (a number or a config path), when and carry", async () => {
  const w = mountForm();
  await w.get("input[value=loop]").setValue(true);
  expect(w.get("[data-wire-apply]").attributes("disabled")).toBeDefined();
  await w.get("input[data-wire=max]").setValue("3");
  await w.get("select[data-wire=when]").setValue("x.again");
  expect(w.get("select[data-wire=carry]").findAll("option").map((o) => o.attributes("value"))).toEqual(["", "verdict", "notes"]);
  await w.get("select[data-wire=carry]").setValue("notes");
  await w.get("[data-wire-apply]").trigger("click");
  expect(w.emitted("apply")).toEqual([["loop", { loop: { max: 3, when: "x.again", carry: "notes" } }]]);
});

test("a branch reads one of the source's outputs and takes a value", async () => {
  const w = mountForm();
  await w.get("input[value=branch]").setValue(true);
  await w.get("select[data-wire=on]").setValue("verdict");
  await w.get("input[data-wire=value]").setValue("yes");
  await w.get("[data-wire-apply]").trigger("click");
  expect(w.emitted("apply")).toEqual([["branch", { branch: { on: "verdict", value: "yes" } }]]);
});

test("for each needs an each box at one end and picks the bundle", async () => {
  expect(mountForm().get("input[value=each]").attributes("disabled")).toBeDefined();
  const w = mountForm({ to: each });
  await w.get("input[value=each]").setValue(true);
  await w.get("select[data-wire=workflow]").setValue("layer");
  await w.get("[data-wire-apply]").trigger("click");
  expect(w.emitted("apply")).toEqual([["each", { each: { workflow: "layer" } }]]);
});

test("an existing wire opens on its move and fields, and can be removed", async () => {
  const w = mountForm({ existing: { from: "check_it", to: "build", kind: "loop", loop: { max: "story.attempts", carry: "notes" } }, removable: true });
  expect((w.get("input[value=loop]").element as HTMLInputElement).checked).toBe(true);
  expect((w.get("input[data-wire=max]").element as HTMLInputElement).value).toBe("story.attempts");
  await w.get("[data-wire-remove]").trigger("click");
  expect(w.emitted("remove")).toHaveLength(1);
});
