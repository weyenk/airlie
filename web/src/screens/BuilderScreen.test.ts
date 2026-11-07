import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { h } from "vue";
import { createMemoryHistory, createRouter, RouterView } from "vue-router";
import { ApiError } from "@/api";
import type { Catalog, WorkflowDef, WorkflowDetail } from "@/api/workflow_defs";
import { STORY_YAML } from "@/test-utils/workflow-yaml";
import BuilderScreen from "./BuilderScreen.vue";

const api = vi.hoisted(() => ({ getWorkflowDef: vi.fn(), listWorkflowDefs: vi.fn(), registryCatalog: vi.fn(), saveDraft: vi.fn(), validateWorkflow: vi.fn() }));
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
const CATALOG: Catalog = {
  roles: ["architect", "coder"],
  types: ["ask", "check"],
  prompts: ["x.fix"],
  parsers: [],
  checks: ["x.intake"],
  predicates: [],
  afters: [],
  turns: [],
  meetings: [],
  prompt_files: [],
};
const FIX_YAML = `workflows:
  fix:
    type: bug
    start: intake
    loose: [card]
    nodes:
      intake: {type: check, run: x.intake, outputs: {report: text}}
      fix: {type: ask, role: coder, prompt: x.fix, inputs: [repro]}
`;
const fixDraft = (over: Partial<WorkflowDetail> = {}) => detail({ name: "fix", type: "bug", source: "draft", yaml: FIX_YAML, prompts: { "fix-brief": "---\nkind: rule\n---\n" }, ...over });
const dialog = () => document.body.querySelector("[role=dialog]") as HTMLElement | null;
const type = (el: Element | null, v: string) => {
  (el as HTMLInputElement).value = v;
  el!.dispatchEvent(new Event("input"));
};

async function open(url = "/workflows/build/story") {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/workflows", component: { template: "<i/>" } },
      { path: "/workflows/build/:name", name: "builder", component: BuilderScreen },
    ],
  });
  await router.push(url);
  // through a RouterView, as the app shows it: the screen's leave guard needs its route
  const w = mount({ render: () => h(RouterView) }, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return { w, router };
}

beforeEach(() => {
  setActivePinia(createPinia());
  api.getWorkflowDef.mockReset().mockResolvedValue(detail());
  api.listWorkflowDefs.mockReset().mockResolvedValue([def("story"), def("layer", { type: null, bundle: true }), def("fix", { source: "draft" })]);
  api.registryCatalog.mockReset().mockResolvedValue(CATALOG);
  api.saveDraft.mockReset().mockImplementation(async (name: string, body: { type: string | null; yaml: string; prompts: Record<string, string> }) =>
    detail({ name, type: body.type, source: "draft", yaml: body.yaml, prompts: body.prompts }),
  );
  api.validateWorkflow.mockReset().mockResolvedValue([]);
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
  expect((panel.get("input[data-input=id]").element as HTMLInputElement).value).toBe("survey");
  expect(panel.text()).toContain("ask");
  expect((panel.get("select[data-input=role]").element as HTMLSelectElement).value).toBe("architect");
  expect((panel.get("[data-input-name=card]").element as HTMLInputElement).checked).toBe(true);
  expect((panel.get("[data-output-row] input").element as HTMLInputElement).value).toBe("survey");
  expect((panel.get("[data-output-row] select").element as HTMLSelectElement).value).toBe("markdown");
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

// ---- editing ----

test("Save on a draft saves it in place: the canvas's YAML and the draft's prompt files", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const { w, router } = await open("/workflows/build/fix");
  await w.get("[data-palette-type=gate]").trigger("click"); // a box added
  await flushPromises();
  expect(w.find("[data-box][data-node=gate-1]").exists()).toBe(true);
  expect(w.get("[data-dirty]").text()).toContain("Unsaved");
  await w.get("[data-save]").trigger("click");
  await flushPromises();
  expect(api.saveDraft).toHaveBeenCalledTimes(1);
  const [name, body] = api.saveDraft.mock.calls[0];
  expect(name).toBe("fix");
  expect(body.type).toBe("bug");
  expect(body.prompts).toEqual({ "fix-brief": "---\nkind: rule\n---\n" });
  expect(body.yaml).toContain("  fix:\n    type: bug\n");
  expect(body.yaml).toContain("      gate-1:\n        type: gate\n");
  expect(body.yaml).toContain("    layout:\n");
  expect(w.find("[data-dirty]").exists()).toBe(false);
  expect(router.currentRoute.value.fullPath).toBe("/workflows/build/fix");
});

test("Save on an unchanged draft sends its own text, comments and all", async () => {
  const text = `# kept\n${FIX_YAML}`;
  api.getWorkflowDef.mockResolvedValue(fixDraft({ yaml: text }));
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-save]").trigger("click");
  await flushPromises();
  expect(api.saveDraft.mock.calls[0][1].yaml).toBe(text);
});

test("Save on a new canvas asks for a kebab-case name and a type first, then opens the saved draft", async () => {
  api.getWorkflowDef.mockImplementation(async (name: string) => detail({ name, type: "bug", source: "draft", yaml: "workflows:\n  my-flow:\n    type: bug\n    start: check-1\n    nodes:\n      check-1: {type: check, run: x.intake}\n" }));
  const { w, router } = await open("/workflows/build/new");
  await w.get("[data-palette-type=check]").trigger("click");
  await w.get("[data-save]").trigger("click");
  await flushPromises();
  expect(api.saveDraft).not.toHaveBeenCalled();
  const d = dialog()!;
  expect(d.textContent).toContain("Save draft");
  type(d.querySelector("[data-save-name]"), "My Flow");
  await flushPromises();
  expect(d.textContent).toContain("kebab-case");
  expect((d.querySelector("[data-save-confirm]") as HTMLButtonElement).disabled).toBe(true);
  type(d.querySelector("[data-save-name]"), "fix"); // taken
  await flushPromises();
  expect(d.textContent).toContain("already");
  type(d.querySelector("[data-save-name]"), "my-flow");
  type(d.querySelector("[data-save-type]"), "bug");
  await flushPromises();
  (d.querySelector("[data-save-confirm]") as HTMLButtonElement).click();
  await flushPromises();
  expect(api.saveDraft).toHaveBeenCalledTimes(1);
  const [name, body] = api.saveDraft.mock.calls[0];
  expect(name).toBe("my-flow");
  expect(body).toMatchObject({ type: "bug", prompts: {} });
  expect(body.yaml).toContain("workflows:\n  my-flow:\n    type: bug\n    start: check-1\n");
  expect(router.currentRoute.value.fullPath).toBe("/workflows/build/my-flow");
  expect(w.get("h1").text()).toBe("my-flow");
});

test("a workflow in the repo's files saves as a draft under a new name", async () => {
  const { w } = await open();
  await w.get("[data-save]").trigger("click");
  await flushPromises();
  expect(api.saveDraft).not.toHaveBeenCalled();
  expect(dialog()!.textContent).toContain("repo's files");
  type(dialog()!.querySelector("[data-save-name]"), "story-two");
  await flushPromises();
  (dialog()!.querySelector("[data-save-confirm]") as HTMLButtonElement).click();
  await flushPromises();
  const [name, body] = api.saveDraft.mock.calls[0];
  expect(name).toBe("story-two");
  expect(body.type).toBe("story");
  expect(body.yaml).toContain("workflows:\n  story-two:\n");
});

test("a save the server refuses as a file's name asks for another", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  api.saveDraft.mockRejectedValueOnce(new ApiError(409, "'fix' is a workflow in the repo's files; a draft needs its own name"));
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-palette-type=gate]").trigger("click");
  await w.get("[data-save]").trigger("click");
  await flushPromises();
  expect(dialog()!.textContent).toContain("a draft needs its own name");
});

test("Validate sends the canvas's YAML and prompt files; a problem outlines its box and a click on it selects the box", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  api.validateWorkflow.mockResolvedValue(["workflow fix: node fix: input 'repro' is not produced by any node and not loose"]);
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-validate]").trigger("click");
  await flushPromises();
  const [yaml, prompts] = api.validateWorkflow.mock.calls[0];
  expect(yaml).toContain("  fix:\n");
  expect(prompts).toEqual({ "fix-brief": "---\nkind: rule\n---\n" });
  expect(w.get("[data-problems]").text()).toContain("1 problem");
  expect(w.get("[data-box][data-node=fix]").attributes("data-problem")).toBe("true");
  expect(w.get("[data-box][data-node=intake]").attributes("data-problem")).toBeUndefined();
  await w.get("[data-problem-node=fix]").trigger("click");
  expect(w.get("[data-box][data-node=fix]").attributes("aria-pressed")).toBe("true");
});

test("a workflow that validates says so", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-validate]").trigger("click");
  await flushPromises();
  expect(w.find("[data-no-problems]").exists()).toBe(true);
});

test("a wire drawn between two boxes opens the wire form; its move lands in the fields", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const { w } = await open("/workflows/build/fix");
  w.findComponent({ name: "AirlieBuilderCanvas" }).vm.$emit("connect", "fix", "intake");
  await flushPromises();
  expect(w.get("[data-wire-form]").text()).toContain("fix → intake");
  await w.get("input[value=loop]").setValue(true);
  await w.get("input[data-wire=max]").setValue("3");
  await w.get("[data-wire-apply]").trigger("click");
  await flushPromises();
  expect(w.find("[data-edge=loop][data-from=fix][data-to=intake]").exists()).toBe(true);
  await w.get("[data-tab=yaml]").trigger("click");
  expect(w.get("[data-yaml]").text()).toContain("loop: {to: intake, max: 3}");
  expect(w.get("[data-yaml-notice]").text()).toContain("Saving regenerates the file");
});

test("a box's form edits the box", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-box][data-node=fix]").trigger("click");
  await w.get("[data-fields] [data-input-name=report]").setValue(true);
  await flushPromises();
  await w.get("[data-tab=yaml]").trigger("click");
  expect(w.get("[data-yaml]").text()).toContain("inputs: [repro, report]");
});

test("leaving with unsaved changes asks first: Stay keeps the canvas, Leave goes", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const { w, router } = await open("/workflows/build/fix");
  await w.get("[data-palette-type=gate]").trigger("click");
  await router.push("/workflows");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/workflows/build/fix");
  expect(dialog()!.textContent).toContain("not saved");
  (dialog()!.querySelector("[data-leave-stay]") as HTMLButtonElement).click();
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/workflows/build/fix");
  expect(w.find("[data-box][data-node=gate-1]").exists()).toBe(true);
  await router.push("/workflows");
  await flushPromises();
  (dialog()!.querySelector("[data-leave-go]") as HTMLButtonElement).click();
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/workflows");
});
