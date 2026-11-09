import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { h } from "vue";
import { createMemoryHistory, createRouter, RouterView } from "vue-router";
import { ApiError } from "@/api";
import type { Catalog, WorkflowDef, WorkflowDetail } from "@/api/workflow_defs";
import { type CreateForm, useCardDraftStore } from "@/stores/card_draft";
import { STORY_YAML } from "@/test-utils/workflow-yaml";
import BuilderScreen from "./BuilderScreen.vue";

const api = vi.hoisted(() => ({ getWorkflowDef: vi.fn(), listWorkflowDefs: vi.fn(), registryCatalog: vi.fn(), publishWorkflow: vi.fn(), saveDraft: vi.fn(), validateWorkflow: vi.fn(), deleteDraft: vi.fn() }));
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
    inputs: [card]
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
      { path: "/board/:key?", name: "board", component: { template: "<i/>" } },
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
  api.publishWorkflow.mockReset().mockResolvedValue({ number: 12, url: "https://github.com/weyenk/airlie/pull/12" });
  api.deleteDraft.mockReset().mockResolvedValue(undefined);
  sessionStorage.clear();
});
afterEach(() => {
  document.body.innerHTML = "";
});

test("the shipped story opens with a box per node and its wires, its name and type on top", async () => {
  const { w } = await open();
  expect(api.getWorkflowDef).toHaveBeenCalledWith("story");
  expect(w.get("h1").text()).toBe("story");
  expect(w.get("[data-workflow-type]").text()).toBe("story");
  expect(w.findAll("[data-canvas] [data-box]")).toHaveLength(22);
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
  expect((panel.get("[data-input-name=request]").element as HTMLInputElement).checked).toBe(true); // v2: the survey reads the request
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

test("the YAML tab shows what Save would send, a hand-written file's comments regenerated away", async () => {
  api.getWorkflowDef.mockResolvedValue(detail({ name: "fix", type: "bug", yaml: `# hand-written\n${FIX_YAML}` }));
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-tab=yaml]").trigger("click");
  expect(w.get("[data-yaml]").text()).not.toContain("# hand-written");
  expect(w.get("[data-yaml]").text()).toContain("intake:");
  expect(w.get("[data-yaml-notice]").text()).toContain("Saving regenerates the file");
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
  api.getWorkflowDef.mockResolvedValue(detail({ name: "fix", type: "bug", yaml: "workflows:\n  fix:\n    nodes:\n      a: {type: check, run: x.a}\n" }));
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
  api.getWorkflowDef.mockImplementation(async (name: string) => detail({ name, type: "bug", source: "draft", yaml: "workflows:\n  my-flow:\n    type: bug\n    nodes:\n      check-1: {type: check, run: x.intake}\n" }));
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
  type(d.querySelector("[data-save-name]"), "fix"); // another draft's
  await flushPromises();
  expect(d.textContent).toContain("already");
  type(d.querySelector("[data-save-name]"), "story"); // a file's: allowed, as an edit of it
  await flushPromises();
  expect(d.querySelector("[data-save-problem]")).toBeNull();
  expect(d.querySelector("[data-save-edits-file]")!.textContent).toContain("edit of it");
  expect((d.querySelector("[data-save-confirm]") as HTMLButtonElement).disabled).toBe(false);
  type(d.querySelector("[data-save-name]"), "my-flow");
  type(d.querySelector("[data-save-type]"), "bug");
  await flushPromises();
  (d.querySelector("[data-save-confirm]") as HTMLButtonElement).click();
  await flushPromises();
  expect(api.saveDraft).toHaveBeenCalledTimes(1);
  const [name, body] = api.saveDraft.mock.calls[0];
  expect(name).toBe("my-flow");
  expect(body).toMatchObject({ type: "bug", prompts: {} });
  expect(body.yaml).toContain("workflows:\n  my-flow:\n    type: bug\n    inputs: [card, repo]\n    nodes:\n      check-1:\n");
  expect(router.currentRoute.value.fullPath).toBe("/workflows/build/my-flow");
  expect(w.get("h1").text()).toBe("my-flow");
});

test("a workflow in the repo's files saves in place, as an edit of that file that Publish then opens", async () => {
  api.saveDraft.mockImplementationOnce(async (name: string, body: { type: string | null; yaml: string; prompts: Record<string, string> }) =>
    detail({ name, type: body.type, source: "draft", yaml: body.yaml, edits_file: true }),
  );
  const { w } = await open();
  expect(w.find("[data-edits-file]").exists()).toBe(false);
  await w.get("[data-box][data-node=intake]").trigger("click"); // a box moved is the usual edit; any will do
  await w.get("[data-box][data-node=intake]").trigger("click");
  await w.get("[data-save]").trigger("click");
  await flushPromises();
  expect(dialog()).toBeNull(); // no name asked: the file's own
  const [name, body] = api.saveDraft.mock.calls[0];
  expect(name).toBe("story");
  expect(body.type).toBe("story");
  expect(body.yaml).toContain("workflows:\n  story:\n");
  expect(w.get("[data-edits-file]").text()).toBe("edit of story");
  expect(publishButton(w).disabled).toBe(false);
  await w.get("[data-publish]").trigger("click");
  await flushPromises();
  expect(api.publishWorkflow).toHaveBeenCalledWith("story");
});

test("a saved edit of a file opens as that edit", async () => {
  api.getWorkflowDef.mockResolvedValue(detail({ source: "draft", edits_file: true }));
  const { w } = await open();
  expect(w.get("[data-edits-file]").text()).toBe("edit of story");
  expect(publishButton(w).disabled).toBe(false);
});

test("a save the server refuses in place asks for another name", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  api.saveDraft.mockRejectedValueOnce(new ApiError(422, "draft workflow 'fix' must hold `workflows:` with the one workflow 'fix'"));
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-palette-type=gate]").trigger("click");
  await w.get("[data-save]").trigger("click");
  await flushPromises();
  expect(dialog()!.textContent).toContain("must hold");
});

test("Delete draft asks first; Keep it keeps it, Delete removes it and goes to the Workflows page", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const { w, router } = await open("/workflows/build/fix");
  await w.get("[data-palette-type=gate]").trigger("click"); // unsaved changes go with the draft: nothing more asked
  await w.get("[data-delete]").trigger("click");
  await flushPromises();
  expect(dialog()!.textContent).toContain("Delete the draft fix?");
  (dialog()!.querySelector("[data-delete-cancel]") as HTMLButtonElement).click();
  await flushPromises();
  expect(api.deleteDraft).not.toHaveBeenCalled();
  await w.get("[data-delete]").trigger("click");
  await flushPromises();
  (dialog()!.querySelector("[data-delete-confirm]") as HTMLButtonElement).click();
  await flushPromises();
  expect(api.deleteDraft).toHaveBeenCalledWith("fix");
  expect(router.currentRoute.value.fullPath).toBe("/workflows");
});

test("a delete that fails stays and says why; a file or a new canvas has no Delete", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  api.deleteDraft.mockRejectedValueOnce(new Error("gone already"));
  const { w, router } = await open("/workflows/build/fix");
  await w.get("[data-delete]").trigger("click");
  await flushPromises();
  (dialog()!.querySelector("[data-delete-confirm]") as HTMLButtonElement).click();
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/workflows/build/fix");
  expect(w.text()).toContain("gone already");
  api.getWorkflowDef.mockResolvedValue(detail());
  await router.push("/workflows/build/story");
  await flushPromises();
  expect(w.find("[data-delete]").exists()).toBe(false);
  await router.push("/workflows/build/new");
  await flushPromises();
  expect(w.find("[data-delete]").exists()).toBe(false);
});

// replaced in Task 5 (the workflow modal): drives WorkflowForm's old start and loose fields
test.skip("with nothing selected the panel edits the workflow's type, loose names and start", async () => {
  const { w } = await open("/workflows/build/new");
  await w.get("[data-palette-type=check]").trigger("click");
  await w.get("[data-palette-type=gate]").trigger("click");
  await w.get("[data-box][data-node=gate-1]").trigger("click"); // unselect
  const panel = w.get("[data-workflow-form]");
  expect(panel.findAll("[data-loose]").map((c) => c.attributes("data-loose"))).toEqual(["card", "repo"]);
  await panel.get("[data-loose-drop=repo]").trigger("click");
  await w.get("[data-workflow-input=start]").setValue("gate-1");
  await w.get("[data-workflow-input=type]").setValue("bug");
  await w.get("[data-tab=yaml]").trigger("click");
  const text = w.get("[data-yaml]").text();
  expect(text).toContain("    type: bug\n    start: gate-1\n    loose: [card]\n");
});

test("a save the server refuses as unreadable (422) reopens the dialog with its reason", async () => {
  api.saveDraft.mockRejectedValueOnce(new ApiError(422, "'drafts' is reserved"));
  const { w } = await open("/workflows/build/new");
  await w.get("[data-palette-type=check]").trigger("click");
  await w.get("[data-save]").trigger("click");
  await flushPromises();
  type(dialog()!.querySelector("[data-save-name]"), "my-flow");
  await flushPromises();
  (dialog()!.querySelector("[data-save-confirm]") as HTMLButtonElement).click();
  await flushPromises();
  expect(dialog()!.textContent).toContain("'drafts' is reserved");
  expect(w.find("[role=alert]").exists()).toBe(false);
});

test("a catalog that did not load says so and retries", async () => {
  api.registryCatalog.mockRejectedValueOnce(new Error("down"));
  const { w } = await open();
  expect(w.get("[data-catalog-failed]").text()).toContain("could not be loaded");
  await w.get("[data-catalog-failed] button").trigger("click");
  await flushPromises();
  expect(api.registryCatalog).toHaveBeenCalledTimes(2);
  expect(w.find("[data-catalog-failed]").exists()).toBe(false);
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

// replaced in Task 5 (the wire modal): drives WireForm's old moves and expects a v1 `loop:` field
test.skip("a wire drawn between two boxes opens the wire form; its move lands in the fields", async () => {
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

// ---- publish ----

const publishButton = (w: Awaited<ReturnType<typeof open>>["w"]) => w.get("[data-publish]").element as HTMLButtonElement;

test("Publish is off while the canvas has problems, is unsaved or is new, and on for a saved draft", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft({ problems: ["workflow fix: node fix: bad"] }));
  const { w, router } = await open("/workflows/build/fix");
  expect(publishButton(w).disabled).toBe(true);

  api.getWorkflowDef.mockResolvedValue(fixDraft());
  await router.push("/workflows/build/new");
  await flushPromises();
  expect(publishButton(w).disabled).toBe(true);
  expect(w.get("[data-publish]").attributes("title")).toBe("Save first");

  await router.push("/workflows/build/fix");
  await flushPromises();
  expect(publishButton(w).disabled).toBe(false);
  await w.get("[data-palette-type=gate]").trigger("click"); // unsaved
  await flushPromises();
  expect(publishButton(w).disabled).toBe(true);
  expect(w.get("[data-publish]").attributes("title")).toBe("Save first");
});

test("a file's workflow is not a draft: Publish waits for a save", async () => {
  const { w } = await open();
  expect(publishButton(w).disabled).toBe(true);
});

test("Publish opens the PR and the bar links to it", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const { w } = await open("/workflows/build/fix");
  expect(w.find("[data-pr]").exists()).toBe(false);
  await w.get("[data-publish]").trigger("click");
  await flushPromises();
  expect(api.publishWorkflow).toHaveBeenCalledWith("fix");
  const link = w.get("a[data-pr]");
  expect(link.text()).toBe("PR #12");
  expect(link.attributes("href")).toBe("https://github.com/weyenk/airlie/pull/12");
  expect(link.attributes("target")).toBe("_blank");
});

test("a publish refused for problems (422) lists them and outlines the boxes", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  api.publishWorkflow.mockRejectedValueOnce(new ApiError(422, JSON.stringify({ problems: ["workflow fix: node fix: input 'repro' is not produced by any node and not loose"] })));
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-publish]").trigger("click");
  await flushPromises();
  expect(w.get("[data-problems]").text()).toContain("1 problem");
  expect(w.get("[data-box][data-node=fix]").attributes("data-problem")).toBe("true");
  expect(w.find("[role=alert]").exists()).toBe(false);
  expect(w.find("[data-pr]").exists()).toBe(false);
});

test("nothing to publish (409) says the trunk already holds it", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  api.publishWorkflow.mockRejectedValueOnce(new ApiError(409, "nothing to publish"));
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-publish]").trigger("click");
  await flushPromises();
  expect(w.get("[data-publish-status]").text()).toBe("Nothing to publish: the trunk already holds this definition");
  expect(w.find("[role=alert]").exists()).toBe(false);
});

test("a publisher failure (502) shows the server's message", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  api.publishWorkflow.mockRejectedValueOnce(new ApiError(502, "GitHub refused the push"));
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-publish]").trigger("click");
  await flushPromises();
  expect(w.get("[role=alert]").text()).toContain("GitHub refused the push");
  expect(publishButton(w).disabled).toBe(false);
});

function later<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((ok, no) => {
    resolve = ok;
    reject = no;
  });
  return { promise, resolve, reject };
}

test("a publish that answers after the address moved on touches nothing: no PR on the other workflow, no trip to the card", async () => {
  const token = useCardDraftStore().hold(HELD);
  const pending = later<{ number: number; url: string }>();
  api.publishWorkflow.mockReturnValueOnce(pending.promise);
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const { w, router } = await open(`/workflows/build/fix?card=${token}`);
  await w.get("[data-publish]").trigger("click");
  api.getWorkflowDef.mockResolvedValue(fixDraft({ name: "other", problems: ["workflow other: node fix: bad"] }));
  await router.push(`/workflows/build/other?card=${token}`);
  await flushPromises();
  pending.resolve({ number: 12, url: "https://github.com/weyenk/airlie/pull/12" });
  await flushPromises();
  expect(router.currentRoute.value.path).toBe("/workflows/build/other");
  expect(w.find("[data-pr]").exists()).toBe(false);
  expect(w.get("[data-problems]").text()).toContain("1 problem");
  expect(useCardDraftStore().take(token)).toEqual(HELD);
});

test.each([
  [422, JSON.stringify({ problems: ["a", "b", "c"] })],
  [409, "nothing to publish"],
  [502, "GitHub refused the push"],
])("a late publish refusal (%i) leaves the workflow now open alone", async (status, message) => {
  const pending = later<{ number: number; url: string }>();
  api.publishWorkflow.mockReturnValueOnce(pending.promise);
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const { w, router } = await open("/workflows/build/fix");
  await w.get("[data-publish]").trigger("click");
  api.getWorkflowDef.mockResolvedValue(fixDraft({ name: "other" }));
  await router.push("/workflows/build/other");
  await flushPromises();
  pending.reject(new ApiError(status, message));
  await flushPromises();
  expect(w.find("[data-problems]").exists()).toBe(false);
  expect(w.find("[data-publish-status]").exists()).toBe(false);
  expect(w.find("[role=alert]").exists()).toBe(false);
});

test("Save and Publish wait for each other", async () => {
  const pending = later<{ number: number; url: string }>();
  api.publishWorkflow.mockReturnValueOnce(pending.promise);
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-publish]").trigger("click");
  expect((w.get("[data-save]").element as HTMLButtonElement).disabled).toBe(true);
  pending.resolve({ number: 12, url: "u" });
  await flushPromises();
  expect((w.get("[data-save]").element as HTMLButtonElement).disabled).toBe(false);

  const saving = later<WorkflowDetail>();
  api.saveDraft.mockReturnValueOnce(saving.promise);
  await w.get("[data-save]").trigger("click");
  expect(publishButton(w).disabled).toBe(true);
  saving.resolve(fixDraft());
  await flushPromises();
  expect(publishButton(w).disabled).toBe(false); // saved: Publish is back
  expect((w.get("[data-save]").element as HTMLButtonElement).disabled).toBe(false);
});

test("a Nothing-to-publish line goes once the canvas is edited", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  api.publishWorkflow.mockRejectedValueOnce(new ApiError(409, "nothing to publish"));
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-publish]").trigger("click");
  await flushPromises();
  expect(w.find("[data-publish-status]").exists()).toBe(true);
  await w.get("[data-palette-type=gate]").trigger("click");
  await flushPromises();
  expect(w.find("[data-publish-status]").exists()).toBe(false);
});

// ---- the trip back to the card ----

const HELD: CreateForm = { level: "story", title: "Half written", description: "Why", parent: "AIR-2", repo: "weyenk/airlie", workflow: "story" };

test("no card, no Cancel", async () => {
  const { w } = await open();
  expect(w.find("[data-cancel]").exists()).toBe(false);
});

test("from a card, Save on a new canvas returns to the board with the card's token and the new workflow chosen", async () => {
  const token = useCardDraftStore().hold(HELD);
  const { w, router } = await open(`/workflows/build/new?card=${token}`);
  await w.get("[data-palette-type=check]").trigger("click");
  await w.get("[data-save]").trigger("click");
  await flushPromises();
  type(dialog()!.querySelector("[data-save-name]"), "my-flow");
  await flushPromises();
  (dialog()!.querySelector("[data-save-confirm]") as HTMLButtonElement).click();
  await flushPromises();
  expect(api.saveDraft).toHaveBeenCalledTimes(1);
  expect(router.currentRoute.value.fullPath).toBe(`/board?create=${token}`);
  expect(useCardDraftStore().take(token)).toEqual({ ...HELD, workflow: "my-flow" });
  expect(dialog()).toBeNull(); // no leave-without-saving question
});

test("from a card, Save on a draft returns to the board with that draft chosen", async () => {
  const token = useCardDraftStore().hold(HELD);
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const { w, router } = await open(`/workflows/build/fix?card=${token}`);
  await w.get("[data-save]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe(`/board?create=${token}`);
  expect(useCardDraftStore().take(token)).toEqual({ ...HELD, workflow: "fix" });
});

test("from a card, a failed save stays in the builder", async () => {
  const token = useCardDraftStore().hold(HELD);
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  api.saveDraft.mockRejectedValueOnce(new ApiError(500, "disk full"));
  const { w, router } = await open(`/workflows/build/fix?card=${token}`);
  await w.get("[data-save]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.path).toBe("/workflows/build/fix");
  expect(useCardDraftStore().take(token)).toEqual(HELD);
});

test("from a card, Publish returns to the board with the workflow chosen", async () => {
  const token = useCardDraftStore().hold(HELD);
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const { w, router } = await open(`/workflows/build/fix?card=${token}`);
  await w.get("[data-publish]").trigger("click");
  await flushPromises();
  expect(api.publishWorkflow).toHaveBeenCalledWith("fix");
  expect(router.currentRoute.value.fullPath).toBe(`/board?create=${token}`);
  expect(useCardDraftStore().take(token)).toEqual({ ...HELD, workflow: "fix" });
});

test("from a card, a failed publish stays in the builder", async () => {
  const token = useCardDraftStore().hold(HELD);
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  api.publishWorkflow.mockRejectedValueOnce(new ApiError(409, "nothing to publish"));
  const { w, router } = await open(`/workflows/build/fix?card=${token}`);
  await w.get("[data-publish]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.path).toBe("/workflows/build/fix");
  expect(useCardDraftStore().take(token)).toEqual(HELD);
});

test("Cancel returns to the card unchanged: nothing saved, nothing published, no question asked", async () => {
  const token = useCardDraftStore().hold(HELD);
  const { w, router } = await open(`/workflows/build/new?card=${token}`);
  await w.get("[data-palette-type=check]").trigger("click"); // unsaved changes
  await w.get("[data-cancel]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe(`/board?create=${token}`);
  expect(dialog()).toBeNull();
  expect(api.saveDraft).not.toHaveBeenCalled();
  expect(api.publishWorkflow).not.toHaveBeenCalled();
  expect(useCardDraftStore().take(token)).toEqual(HELD);
});
