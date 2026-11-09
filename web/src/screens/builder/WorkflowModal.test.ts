import { flushPromises, mount } from "@vue/test-utils";
import WorkflowModal from "./WorkflowModal.vue";

const mountModal = (over: Partial<{ type: string | null; inputs: string[]; outputs: string[] }> = {}) =>
  mount(WorkflowModal, { props: { open: true, type: "story", inputs: ["card", "repo"], outputs: [], types: ["story", "bug"], ...over }, attachTo: document.body });
const $ = <T extends Element = HTMLElement>(sel: string) => document.body.querySelector<T>(sel);
const updates = (w: ReturnType<typeof mountModal>) => (w.emitted("update") ?? []).map((e) => e[0]);
const type = (el: Element | null, v: string) => {
  (el as HTMLInputElement).value = v;
  el!.dispatchEvent(new Event("input"));
};

afterEach(() => {
  document.body.innerHTML = "";
});

test("the inputs and outputs are chips; the type is a select whose blank option is a bundle", async () => {
  mountModal();
  await flushPromises();
  expect([...document.body.querySelectorAll("[data-workflow-inputs] [data-chip]")].map((c) => c.getAttribute("data-chip"))).toEqual(["card", "repo"]);
  expect($("[data-workflow-outputs]")!.textContent).toContain("none");
  const select = $<HTMLSelectElement>("[data-workflow-type-select]")!;
  expect(select.value).toBe("story");
  expect([...select.options].map((o) => o.value)).toEqual(["", "story", "bug"]);
});

test("changing the type hands it up; blank is a bundle (null)", async () => {
  const w = mountModal();
  await flushPromises();
  const select = $<HTMLSelectElement>("[data-workflow-type-select]")!;
  select.value = "bug";
  select.dispatchEvent(new Event("change"));
  select.value = "";
  select.dispatchEvent(new Event("change"));
  expect(updates(w)).toEqual([{ type: "bug" }, { type: null }]);
});

test("a name typed and entered joins the inputs; a chip's x removes one; a bad name says why", async () => {
  const w = mountModal();
  await flushPromises();
  type($("[data-workflow-add=inputs]"), "ticket");
  $("[data-workflow-add=inputs]")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
  $<HTMLButtonElement>("[data-workflow-inputs] [data-chip-drop=repo]")!.click();
  type($("[data-workflow-add=inputs]"), "not a name");
  $("[data-workflow-add=inputs]")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
  await flushPromises();
  expect(updates(w)).toEqual([{ inputs: ["card", "repo", "ticket"] }, { inputs: ["card"] }]);
  expect($("[data-workflow-problem=inputs]")!.textContent).toContain("not a name");
});

test("outputs take a name.{item} for a list run per item; closing adds the name being typed", async () => {
  const w = mountModal({ type: null, outputs: ["drafted"] });
  await flushPromises();
  type($("[data-workflow-add=outputs]"), "built.{item}");
  $<HTMLButtonElement>("[data-workflow-done]")!.click();
  await flushPromises();
  expect(updates(w)).toEqual([{ outputs: ["drafted", "built.{item}"] }]);
  expect(w.emitted("update:open")?.at(-1)).toEqual([false]);
});
