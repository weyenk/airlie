import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { ApiError } from "@/api";
import type { WorkflowDef, WorkflowDetail } from "@/api/workflow_defs";
import { STORY_YAML } from "@/test-utils/workflow-yaml";
import BuilderScreen from "./BuilderScreen.vue";

const api = vi.hoisted(() => ({ getWorkflowDef: vi.fn(), listWorkflowDefs: vi.fn() }));
vi.mock("@/api/workflow_defs", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflow_defs")>()), ...api }));

const detail = (over: Partial<WorkflowDetail> = {}): WorkflowDetail => ({
  name: "story",
  type: "story",
  source: "file",
  yaml: STORY_YAML,
  layout: {},
  problems: [],
  prompts: {},
  pr: null,
  ...over,
});
const def = (name: string, over: Partial<WorkflowDef> = {}): WorkflowDef => ({ name, type: "story", source: "file", problems: [], pr: null, ...over });

async function open(url = "/workflows/build/story") {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/workflows", component: { template: "<i/>" } },
      { path: "/workflows/build/:name", name: "builder", component: BuilderScreen },
    ],
  });
  await router.push(url);
  const w = mount(BuilderScreen, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return { w, router };
}

beforeEach(() => {
  setActivePinia(createPinia());
  api.getWorkflowDef.mockReset().mockResolvedValue(detail());
  api.listWorkflowDefs.mockReset().mockResolvedValue([def("story"), def("layer", { type: null, bundle: true }), def("fix", { source: "draft" })]);
});
afterEach(() => {
  document.body.innerHTML = "";
});

test("the shipped story opens with a box per node and its wires, its name and type on top", async () => {
  const { w } = await open();
  expect(api.getWorkflowDef).toHaveBeenCalledWith("story");
  expect(w.get("h1").text()).toBe("story");
  expect(w.get("[data-workflow-type]").text()).toBe("story");
  expect(w.findAll("[data-canvas] [data-box]")).toHaveLength(19);
  expect(w.findAll("[data-canvas] [data-edge]").length).toBeGreaterThan(15);
  expect(w.get("a[href='/workflows']").text()).toContain("Workflows");
});

test("the palette lists the other definitions as bundles", async () => {
  const { w } = await open();
  expect(w.findAll("[data-bundle]").map((b) => b.text())).toEqual(["layer", "fix"]);
});

test("a saved layout places the boxes", async () => {
  api.getWorkflowDef.mockResolvedValue(detail({ layout: { intake: { x: 640, y: 180 } } }));
  const { w } = await open();
  expect(w.get("[data-box][data-node=intake]").attributes("style")).toContain("left: 664px");
});

test("a click on a box shows its fields in the side panel", async () => {
  const { w } = await open();
  expect(w.get("[data-fields]").text()).toContain("Select a box");
  await w.get("[data-box][data-node=survey]").trigger("click");
  const panel = w.get("[data-fields]");
  expect(panel.text()).toContain("survey");
  expect(panel.get("[data-field=type]").text()).toContain("ask");
  expect(panel.get("[data-field=role]").text()).toContain("architect");
  expect(panel.get("[data-field=inputs]").text()).toContain("card");
  expect(panel.get("[data-field=outputs]").text()).toContain("survey: markdown");
  expect(w.get("[data-box][data-node=survey]").attributes("aria-pressed")).toBe("true");
});

test("the YAML tab shows the definition's text", async () => {
  const { w } = await open();
  expect(w.find("[data-yaml]").exists()).toBe(false);
  await w.get("[data-tab=yaml]").trigger("click");
  expect(w.get("[data-yaml]").text()).toContain("take_card_as_story:");
  expect(w.find("[data-canvas]").exists()).toBe(false);
  await w.get("[data-tab=diagram]").trigger("click");
  expect(w.find("[data-canvas]").exists()).toBe(true);
});

test("new is an empty canvas, nothing fetched for it", async () => {
  const { w } = await open("/workflows/build/new");
  expect(api.getWorkflowDef).not.toHaveBeenCalled();
  expect(w.get("h1").text()).toBe("new");
  expect(w.findAll("[data-box]")).toHaveLength(0);
  expect(w.text()).toContain("No boxes yet");
});

test("a definition that is not there says so", async () => {
  api.getWorkflowDef.mockRejectedValue(new ApiError(404, "no workflow 'nope'"));
  const { w } = await open("/workflows/build/nope");
  expect(w.get("[role=alert]").text()).toContain("no workflow 'nope'");
});

test("text that does not parse says so and still shows on the YAML tab", async () => {
  api.getWorkflowDef.mockResolvedValue(detail({ yaml: "workflows: [\n" }));
  const { w } = await open();
  expect(w.get("[role=alert]").text()).toContain("could not be drawn");
  await w.get("[data-tab=yaml]").trigger("click");
  expect(w.get("[data-yaml]").text()).toContain("workflows: [");
});

test("a problem count shows when the definition has problems", async () => {
  api.getWorkflowDef.mockResolvedValue(detail({ problems: ["workflow story: node x: bad", "workflow story: node y: bad"], source: "draft" }));
  const { w } = await open();
  expect(w.get("[data-problems]").text()).toContain("2 problems");
  expect(w.text()).toContain("draft");
});

test("another name in the address loads that definition", async () => {
  const { w, router } = await open();
  api.getWorkflowDef.mockResolvedValue(detail({ name: "fix", type: "bug", yaml: "workflows:\n  fix:\n    start: a\n    nodes:\n      a: {type: check, run: x.a}\n" }));
  await router.push("/workflows/build/fix");
  await flushPromises();
  expect(api.getWorkflowDef).toHaveBeenLastCalledWith("fix");
  expect(w.get("h1").text()).toBe("fix");
  expect(w.findAll("[data-box]")).toHaveLength(1);
});

test("leaving the builder asks for nothing more", async () => {
  const { router } = await open();
  await router.push("/workflows");
  await flushPromises();
  expect(api.getWorkflowDef).toHaveBeenCalledTimes(1);
});
