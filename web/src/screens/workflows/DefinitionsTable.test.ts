import { flushPromises, mount } from "@vue/test-utils";
import { answerConfirm, confirmState } from "@/composables/useConfirm";
import { createMemoryHistory, createRouter } from "vue-router";
import { ApiError } from "@/api";
import type { WorkflowDef } from "@/api/workflow_defs";
import DefinitionsTable from "./DefinitionsTable.vue";

const api = vi.hoisted(() => ({ deleteDraft: vi.fn() }));
vi.mock("@/api/workflow_defs", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflow_defs")>()), ...api }));

const def = (name: string, over: Partial<WorkflowDef> = {}): WorkflowDef => ({ name, type: "story", source: "file", problems: [], pr: null, used_by: [], ...over });
const DEFS = [def("story"), def("layer", { type: null, bundle: true, used_by: ["story", "fix"] }), def("fix", { source: "draft", type: "bug" })];

async function open(defs = DEFS) {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:rest(.*)*", component: { template: "<i/>" } }] });
  await router.push("/workflows");
  const w = mount(DefinitionsTable, { props: { defs, failed: false }, global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return w;
}
const confirmOk = async () => {
  answerConfirm(true);
  await flushPromises();
};

beforeEach(() => api.deleteDraft.mockReset().mockResolvedValue(undefined));
afterEach(() => {
  document.body.innerHTML = "";
});

test("each row says which workflows run it", async () => {
  const w = await open();
  expect(w.get("[data-def=layer] [data-def-used-by]").text()).toBe("story, fix");
  expect(w.get("[data-def=story] [data-def-used-by]").text()).toBe("");
});

test("only a draft can be deleted", async () => {
  const w = await open();
  expect(w.find("[data-def=story] [data-def-delete]").exists()).toBe(false);
  expect(w.find("[data-def=layer] [data-def-delete]").exists()).toBe(false);
  expect(w.find("[data-def=fix] [data-def-delete]").exists()).toBe(true);
});

test("a delete asks first, and Cancel keeps the draft", async () => {
  const w = await open();
  await w.get("[data-def=fix] [data-def-delete]").trigger("click");
  // the confirm dialog is the shell's (one for the app): the table asks it
  expect(confirmState.open).toBe(true);
  expect(confirmState.title).toBe("Delete the draft fix?");
  answerConfirm(false);
  await flushPromises();
  expect(api.deleteDraft).not.toHaveBeenCalled();
});

test("a confirmed delete removes the draft and tells the page", async () => {
  const w = await open();
  await w.get("[data-def=fix] [data-def-delete]").trigger("click");
  await confirmOk();
  expect(api.deleteDraft).toHaveBeenCalledWith("fix");
  expect(w.findComponent(DefinitionsTable).emitted("deleted")).toEqual([["fix"]]);
  expect(w.find("[data-def-refused]").exists()).toBe(false);
});

test("a draft another workflow runs refuses the delete, naming those workflows", async () => {
  api.deleteDraft.mockRejectedValueOnce(new ApiError(409, JSON.stringify({ used_by: ["story", "layer"] })));
  const w = await open();
  await w.get("[data-def=fix] [data-def-delete]").trigger("click");
  await confirmOk();
  expect(w.get("[data-def=fix] [data-def-refused]").text()).toBe("used by: story, layer");
  expect(w.findComponent(DefinitionsTable).emitted("deleted")).toBeUndefined();
});

test("any other failure is shown on the row too", async () => {
  api.deleteDraft.mockRejectedValueOnce(new Error("gone already"));
  const w = await open();
  await w.get("[data-def=fix] [data-def-delete]").trigger("click");
  await confirmOk();
  expect(w.get("[data-def=fix] [data-def-refused]").text()).toBe("gone already");
});
