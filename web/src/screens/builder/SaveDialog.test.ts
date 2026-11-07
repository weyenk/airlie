import { flushPromises, mount } from "@vue/test-utils";
import SaveDialog from "./SaveDialog.vue";

afterEach(() => {
  document.body.innerHTML = "";
});

const typeName = async (v: string) => {
  const el = document.body.querySelector("[data-save-name]") as HTMLInputElement;
  el.value = v;
  el.dispatchEvent(new Event("input"));
  await flushPromises();
};

test.each(["new", "catalog", "validate", "drafts"])("the name %s is reserved and cannot be saved under", async (name) => {
  const w = mount(SaveDialog, { props: { open: true, taken: [], types: [], type: null }, attachTo: document.body });
  await flushPromises();
  await typeName(name);
  expect(document.body.querySelector("[data-save-problem]")!.textContent).toContain(`'${name}' is reserved`);
  expect((document.body.querySelector("[data-save-confirm]") as HTMLButtonElement).disabled).toBe(true);
  await typeName("my-flow");
  expect(document.body.querySelector("[data-save-problem]")).toBeNull();
  (document.body.querySelector("[data-save-confirm]") as HTMLButtonElement).click();
  expect(w.emitted("save")).toEqual([["my-flow", null]]);
});
