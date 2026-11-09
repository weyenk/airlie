import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { h } from "vue";
import { createMemoryHistory, createRouter, RouterView } from "vue-router";
import { ApiError } from "@/api";
import type { Catalog, WorkflowDef, WorkflowDetail } from "@/api/workflow_defs";
import { type CreateForm, useCardDraftStore } from "@/stores/card_draft";
import { LAYER_YAML, STORY_YAML } from "@/test-utils/workflow-yaml";
import BuilderScreen from "./BuilderScreen.vue";
import { takeGraph } from "./builder/builder_state";

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
const def = (name: string, over: Partial<WorkflowDef> = {}): WorkflowDef => ({ name, type: "story", source: "file", problems: [], pr: null, used_by: [], ...over });
const CATALOG: Catalog = {
  roles: ["architect", "coder", "product", "tester"],
  types: ["ask", "check"],
  formats: ["text"],
  kinds: ["text"],
  checks: [{ name: "story.intake", title: "Intake", inputs: ["card", "repo"], outputs: ["report"], formats: { report: "text" }, transient: [], every_run: false, args: [] }],
  meetings: [{ roles: ["product", "architect"], inputs: ["card"] }],
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

test("a click on a box selects it; its card's Edit opens its fields in the modal", async () => {
  const { w } = await open();
  await w.get("[data-box][data-node=survey]").trigger("click");
  expect(w.get("[data-box][data-node=survey]").attributes("aria-pressed")).toBe("true");
  expect(document.body.querySelector("[data-node-modal]")).toBeNull(); // a click only selects
  expect(w.find("[data-fields]").exists()).toBe(false); // there is no side panel: the canvas has the room
  await w.get("[data-box-card][data-node=survey] [data-box-edit]").trigger("click");
  await flushPromises();
  const modal = document.body.querySelector("[data-node-modal]")!;
  expect((modal.querySelector("input[data-input=id]") as HTMLInputElement).value).toBe("survey");
  expect(modal.querySelector("[data-node-type]")!.textContent).toBe("ask");
  expect((modal.querySelector("select[data-input=role]") as HTMLSelectElement).value).toBe("architect");
  expect((modal.querySelector("[data-input-name=request]") as HTMLInputElement).checked).toBe(true); // v2: the survey reads the request
  expect((modal.querySelector("select[data-input=prompt-file]") as HTMLSelectElement).value).toBe("prompt:story-survey"); // the story names a prompt file
  expect((modal.querySelector("[data-input='events.done']") as HTMLInputElement).value).toBe("survey_done");
  expect((modal.querySelector("[data-output-row] [data-output-name]") as HTMLInputElement).value).toBe("survey");
  expect((modal.querySelector("[data-output-row] [data-output-format]") as HTMLSelectElement).value).toBe("markdown");
  (modal.querySelector("[data-node-done]") as HTMLButtonElement).click();
  await flushPromises();
  expect(document.body.querySelector("[data-node-modal]")).toBeNull();
});

/** The fix box's modal open, its prompt focused and typed into (no blur yet). */
async function typingInFix() {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const opened = await open("/workflows/build/fix");
  await opened.w.get("[data-box][data-node=fix]").trigger("dblclick");
  await flushPromises();
  const area = document.body.querySelector("[data-node-modal] [data-input=prompt]") as HTMLTextAreaElement;
  area.focus();
  type(area, "Fix it {repro}.");
  return opened;
}
async function expectFixPromptSaved(w: Awaited<ReturnType<typeof open>>["w"]) {
  expect(document.body.querySelector("[data-node-modal]")).toBeNull();
  expect(w.find("[role=alert]").exists()).toBe(false); // no error banner (it once said "no box null")
  await w.get("[data-tab=yaml]").trigger("click");
  expect(w.get("[data-yaml]").text()).toContain("prompt: Fix it {repro}.");
}

test("Escape after typing in the prompt keeps the text, and closes with no error", async () => {
  const { w } = await typingInFix();
  document.body.querySelector("[data-node-modal]")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await flushPromises();
  await expectFixPromptSaved(w);
});

test("a click outside after typing in the prompt keeps the text, and closes with no error", async () => {
  const { w } = await typingInFix();
  // reka's outside-pointer check registers a tick after opening
  await new Promise((r) => setTimeout(r, 0));
  document.body.dispatchEvent(new Event("pointerdown", { bubbles: true }));
  await flushPromises();
  await expectFixPromptSaved(w);
});

test("an output renamed in the modal is renamed in its readers too", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const { w } = await open("/workflows/build/fix");
  w.findComponent({ name: "AirlieBuilderCanvas" }).vm.$emit("open", "intake");
  await flushPromises();
  w.findComponent({ name: "AirlieBuilderOutputsEditor" }).vm.$emit("rename", "report", "findings");
  await flushPromises();
  await w.get("[data-tab=yaml]").trigger("click");
  expect(w.get("[data-yaml]").text()).toContain("outputs: {findings: text}");
});


test("a line drawn from an artifact port is made at once and carries that artifact", async () => {
  const { w } = await open();
  w.findComponent({ name: "AirlieBuilderCanvas" }).vm.$emit("connect", "survey", "refine", { artifacts: ["survey"] });
  await flushPromises();
  expect(document.body.querySelector("[data-wire-modal]")).toBeNull();
  expect(w.get("[data-edge-hit][data-from=survey][data-to=refine] title").text()).toMatch(/^survey: survey/);
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

test("a delete the server refuses (409) names the workflows that use the draft and stays", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  api.deleteDraft.mockRejectedValueOnce(new ApiError(409, JSON.stringify({ used_by: ["story", "layer"] })));
  const { w, router } = await open("/workflows/build/fix");
  await w.get("[data-delete]").trigger("click");
  await flushPromises();
  (dialog()!.querySelector("[data-delete-confirm]") as HTMLButtonElement).click();
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/workflows/build/fix");
  expect(w.text()).toContain("used by story, layer");
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

test("a box's modal edits the box as the change is made", async () => {
  api.getWorkflowDef.mockResolvedValue(fixDraft());
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-box][data-node=fix]").trigger("dblclick");
  await flushPromises();
  const report = document.body.querySelector("[data-node-modal] [data-input-name=report]") as HTMLInputElement;
  report.checked = true;
  report.dispatchEvent(new Event("change"));
  await flushPromises();
  (document.body.querySelector("[data-node-done]") as HTMLButtonElement).click();
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

// the wire modal and the workflow modal
const LINED_YAML = `workflows:
  fix:
    type: bug
    inputs: [card]
    nodes:
      intake: {type: check, run: x.intake, outputs: {report: text, verdict: text}, to: [fix]}
      fix: {type: ask, role: coder, prompt: x.fix, inputs: [report], outputs: {fixed: text}}
`;
const lined = (over: Partial<WorkflowDetail> = {}) => fixDraft({ yaml: LINED_YAML, ...over });
const modal = (sel: string) => document.body.querySelector<HTMLElement>(`[data-wire-modal] ${sel}`);
const yamlText = async (w: Awaited<ReturnType<typeof open>>["w"]) => {
  await w.get("[data-tab=yaml]").trigger("click");
  return w.get("[data-yaml]").text();
};

test("the screen has a palette and a canvas and no Fields aside", async () => {
  const { w } = await open();
  expect(w.findAll("aside").map((a) => a.attributes("aria-label"))).toEqual(["Palette"]);
  expect(w.find("[data-fields]").exists()).toBe(false);
  const grid = w.get("[data-canvas]").element.closest("[class*='grid-cols']")!;
  expect(grid.className).toContain("grid-cols-[180px_minmax(0,1fr)]");
  expect(grid.className).not.toContain("280px");
});

test("a wire's artifacts and condition save into the line", async () => {
  api.getWorkflowDef.mockResolvedValue(lined());
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-edge-hit][data-from=intake][data-to=fix]").trigger("dblclick");
  await flushPromises();
  expect(modal("[data-wire-artifact=report]")).not.toBeNull();
  (modal("[data-wire-artifact=verdict]") as HTMLInputElement).click();
  type(modal("[data-wire-when]"), "verdict == pass");
  await flushPromises();
  (modal("[data-wire-apply]") as HTMLButtonElement).click();
  await flushPromises();
  expect(document.body.querySelector("[data-wire-modal]")).toBeNull();
  expect(w.get("[data-edge-label]").text()).toBe("if verdict == pass · report, verdict");
  const text = await yamlText(w);
  expect(text).toContain("when: verdict == pass");
  expect(text).toContain("inputs: [report, verdict]");
});

test("unchecking an unreferenced, single-source artifact removes it from the target's inputs and from the YAML", async () => {
  api.getWorkflowDef.mockResolvedValue(lined());
  const { w } = await open("/workflows/build/fix");
  expect(await yamlText(w)).toContain("inputs: [report]");
  await w.get("[data-tab=diagram]").trigger("click");
  await w.get("[data-edge-hit][data-from=intake][data-to=fix]").trigger("dblclick");
  await flushPromises();
  (modal("[data-wire-artifact=report]") as HTMLInputElement).click();
  await flushPromises();
  expect(modal("[data-wire-unread]")).toBeNull();
  (modal("[data-wire-apply]") as HTMLButtonElement).click();
  await flushPromises();
  expect(await yamlText(w)).not.toContain("inputs: [report]");
});

test("clearing the condition removes the key from the YAML", async () => {
  api.getWorkflowDef.mockResolvedValue(lined({ yaml: LINED_YAML.replace("to: [fix]", "to: [{node: fix, when: verdict == pass}]") }));
  const { w } = await open("/workflows/build/fix");
  expect(await yamlText(w)).toContain("when: verdict == pass");
  await w.get("[data-tab=diagram]").trigger("click");
  await w.get("[data-edge-hit][data-from=intake][data-to=fix]").trigger("dblclick");
  await flushPromises();
  type(modal("[data-wire-when]"), "");
  await flushPromises();
  (modal("[data-wire-apply]") as HTMLButtonElement).click();
  await flushPromises();
  expect(await yamlText(w)).not.toContain("when:");
});

test("a loop with max 2 drawn from the wire modal draws as a loop and saves into the line", async () => {
  api.getWorkflowDef.mockResolvedValue(lined());
  const { w } = await open("/workflows/build/fix");
  w.findComponent({ name: "AirlieBuilderCanvas" }).vm.$emit("connect", "fix", "intake", {});
  await flushPromises();
  expect(document.body.querySelector("[data-wire-modal]")).toBeNull(); // the line is drawn at once
  expect(w.find("[data-edge=line][data-from=fix][data-to=intake]").exists()).toBe(true);
  // the drawn line is selected: its card's Edit opens the modal
  await w.get("[data-edge-card] [data-edge-edit]").trigger("click");
  await flushPromises();
  (modal("[data-wire-loop]") as HTMLInputElement).click();
  await flushPromises();
  type(modal("[data-wire-max]"), "2");
  await flushPromises();
  (modal("[data-wire-apply]") as HTMLButtonElement).click();
  await flushPromises();
  expect(w.find("[data-edge=loop][data-from=fix][data-to=intake]").exists()).toBe(true);
  expect(await yamlText(w)).toContain("loop: {max: 2}");
});

test("a condition problem from the server marks the wire's label and lists in the problems; the draft still saves", async () => {
  api.getWorkflowDef.mockResolvedValue(lined());
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-validate]").trigger("click"); // the graph has been validated: an edit is checked again
  await flushPromises();
  api.validateWorkflow.mockResolvedValue(["workflow fix: node intake: line to fix: condition names 'zzz', which nothing produces"]);
  await w.get("[data-edge-hit][data-from=intake][data-to=fix]").trigger("dblclick");
  await flushPromises();
  type(modal("[data-wire-when]"), "zzz == 1");
  await flushPromises();
  (modal("[data-wire-apply]") as HTMLButtonElement).click();
  await flushPromises();
  expect(api.validateWorkflow).toHaveBeenCalledTimes(2);
  expect(api.validateWorkflow.mock.calls[1][0]).toContain("zzz == 1");
  expect(w.get("[data-edge-label]").attributes("data-edge-problem")).toBe("true");
  expect(w.get("[data-problem-edge='intake>fix']").text()).toContain("nothing produces");
  expect(w.get("[data-save]").attributes("disabled")).toBeUndefined();
  // a click on the problem opens the line's modal
  await w.get("[data-problem-edge='intake>fix']").trigger("click");
  await flushPromises();
  expect(modal("[data-wire-when]")).not.toBeNull();
  (modal("[data-wire-done]") as HTMLButtonElement).click();
  await flushPromises();
  await w.get("[data-save]").trigger("click");
  await flushPromises();
  expect(api.saveDraft).toHaveBeenCalledTimes(1);
});

test("Remove in the wire modal takes the line out", async () => {
  api.getWorkflowDef.mockResolvedValue(lined());
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-edge-hit][data-from=intake][data-to=fix]").trigger("dblclick");
  await flushPromises();
  (modal("[data-wire-remove]") as HTMLButtonElement).click();
  await flushPromises();
  expect(w.find("[data-edge][data-from=intake]").exists()).toBe(false);
  expect(document.body.querySelector("[data-wire-modal]")).toBeNull();
});

test("the title bar opens the workflow modal, which edits the inputs and outputs and the type", async () => {
  api.getWorkflowDef.mockResolvedValue(lined());
  const { w } = await open("/workflows/build/fix");
  await w.get("[data-workflow-edit]").trigger("click");
  await flushPromises();
  const m = document.body.querySelector("[data-workflow-modal]")!;
  expect([...m.querySelectorAll("[data-workflow-inputs] [data-chip]")].map((c) => c.getAttribute("data-chip"))).toEqual(["card"]);
  const add = m.querySelector("[data-workflow-add=inputs]")!;
  type(add, "repo");
  add.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
  const out = m.querySelector("[data-workflow-add=outputs]")!;
  type(out, "fixed");
  out.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
  const select = m.querySelector<HTMLSelectElement>("[data-workflow-type-select]")!;
  select.value = "";
  select.dispatchEvent(new Event("change"));
  await flushPromises();
  (m.querySelector("[data-workflow-done]") as HTMLButtonElement).click();
  await flushPromises();
  const text = await yamlText(w);
  expect(text).toContain("inputs: [card, repo]");
  expect(text).toContain("outputs: [fixed]");
  expect(text).not.toContain("type: bug");
  expect(w.get("[data-workflow-type]").text()).toBe("bug"); // the title bar shows the saved definition's type until a save
});

test("a draft the server upgraded from the old schema says so until it is saved", async () => {
  api.getWorkflowDef.mockResolvedValue(lined({ upgraded: true }));
  const { w } = await open("/workflows/build/fix");
  expect(w.get("[data-upgraded]").text()).toContain("upgraded to the current schema");
  await w.get("[data-save]").trigger("click");
  await flushPromises();
  expect(w.find("[data-upgraded]").exists()).toBe(false);
});

test("a draft that was not upgraded shows no cue", async () => {
  api.getWorkflowDef.mockResolvedValue(lined());
  const { w } = await open("/workflows/build/fix");
  expect(w.find("[data-upgraded]").exists()).toBe(false);
});

// bundles in place: a bundle box opens the workflow it runs, and the breadcrumb comes back with the canvas as it was
const withLayer = () =>
  api.getWorkflowDef.mockImplementation(async (n: string) => (n === "layer" ? detail({ name: "layer", type: null, yaml: LAYER_YAML }) : detail()));
const crumb = (w: Awaited<ReturnType<typeof open>>["w"]) => w.find("[data-breadcrumb]");

test("a double-click on a bundle box opens its workflow with a token, and the breadcrumb names the parent", async () => {
  withLayer();
  const { w, router } = await open();
  expect(crumb(w).exists()).toBe(false); // not inside a bundle
  await w.get("[data-box][data-node=build_layers]").trigger("dblclick");
  await flushPromises();
  const at = router.currentRoute.value;
  expect(at.name).toBe("builder");
  expect(at.params.name).toBe("layer");
  expect(typeof at.query.from).toBe("string");
  expect(document.body.querySelector("[data-node-modal]")).toBeNull(); // a double-click on a bundle box enters it
  expect(w.get("h1").text()).toBe("layer");
  expect(crumb(w).text()).toContain("story");
  const held = takeGraph(at.query.from as string); // the token resolves to the parent as it was
  expect(held?.name).toBe("story");
  expect(held?.graph.nodes).toHaveLength(22);
  expect(held?.graph.nodes.some((n) => n.id === "build_layers")).toBe(true);
});

test("the card's Edit on a bundle box opens its fields, not the bundle", async () => {
  withLayer();
  const { w, router } = await open();
  await w.get("[data-box][data-node=build_layers]").trigger("click");
  await w.get("[data-box-card][data-node=build_layers] [data-box-edit]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.params.name).toBe("story");
  const modal = document.body.querySelector("[data-node-modal]")!;
  expect((modal.querySelector("select[data-input=workflow]") as HTMLSelectElement).value).toBe("layer");
  expect((modal.querySelector("select[data-input=each]") as HTMLSelectElement).value).toBe("layers");
});

test("the modal's Open button enters the bundle", async () => {
  withLayer();
  const { w, router } = await open();
  await w.get("[data-box][data-node=build_layers]").trigger("click");
  await w.get("[data-box-card][data-node=build_layers] [data-box-edit]").trigger("click");
  await flushPromises();
  expect(document.body.querySelector("[data-bundle-open]")!.textContent).toContain("Open layer");
  (document.body.querySelector("[data-bundle-open]") as HTMLButtonElement).click();
  await flushPromises();
  expect(router.currentRoute.value.params.name).toBe("layer");
  expect(crumb(w).text()).toContain("story");
});

test("browser Back from the bundle restores the parent's unsaved edits", async () => {
  withLayer();
  const { w, router } = await open();
  await w.get("[data-palette-type=gate]").trigger("click");
  await w.get("[data-box][data-node=build_layers]").trigger("dblclick");
  await flushPromises();
  expect(router.currentRoute.value.params.name).toBe("layer");
  router.back();
  await flushPromises();
  expect(router.currentRoute.value.params.name).toBe("story");
  expect(w.findAll("[data-canvas] [data-box]")).toHaveLength(23);
  expect(w.get("[data-dirty]").text()).toContain("Unsaved");
});

test("a browser that cannot hold the canvas keeps a dirty one here with a warning, and a clean one goes", async () => {
  withLayer();
  const { w, router } = await open();
  const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("quota");
  });
  try {
    await w.get("[data-palette-type=gate]").trigger("click");
    await w.get("[data-box][data-node=build_layers]").trigger("dblclick");
    await flushPromises();
    expect(router.currentRoute.value.params.name).toBe("story");
    expect(w.get("[role=alert]").text()).toContain("could not hold your unsaved edits; save first");
    expect(w.findAll("[data-canvas] [data-box]")).toHaveLength(23);
  } finally {
    spy.mockRestore();
  }
  const clean = await open();
  const spy2 = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("quota");
  });
  try {
    await clean.w.get("[data-box][data-node=build_layers]").trigger("dblclick");
    await flushPromises();
    expect(clean.router.currentRoute.value.params.name).toBe("layer");
    expect(clean.router.currentRoute.value.query.parent).toBe("story");
  } finally {
    spy2.mockRestore();
  }
});

test("the card a builder came from goes into the bundle and back", async () => {
  withLayer();
  const { w, router } = await open("/workflows/build/story?card=tok");
  await w.get("[data-box][data-node=build_layers]").trigger("dblclick");
  await flushPromises();
  expect(router.currentRoute.value.query.card).toBe("tok");
  await crumb(w).trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.params.name).toBe("story");
  expect(router.currentRoute.value.query.card).toBe("tok");
});

test("a bundle inside a bundle: the breadcrumb restores each level in turn", async () => {
  const MID = `workflows:
  layer:
    inputs: [card]
    nodes:
      intake: {type: check, run: x.intake, outputs: {report: text}}
      inner: {type: bundle, workflow: fix}
`;
  api.getWorkflowDef.mockImplementation(async (n: string) =>
    n === "layer" ? detail({ name: "layer", type: null, yaml: MID }) : n === "fix" ? fixDraft() : detail(),
  );
  const { w, router } = await open();
  await w.get("[data-palette-type=gate]").trigger("click"); // unsaved in story: 23 boxes
  await w.get("[data-box][data-node=build_layers]").trigger("dblclick");
  await flushPromises();
  await w.get("[data-palette-type=gate]").trigger("click"); // unsaved in layer: 3 boxes
  expect(w.findAll("[data-canvas] [data-box]")).toHaveLength(3);
  await w.get("[data-box][data-node=inner]").trigger("dblclick");
  await flushPromises();
  expect(router.currentRoute.value.params.name).toBe("fix");
  expect(crumb(w).text()).toContain("layer");
  await crumb(w).trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.params.name).toBe("layer");
  expect(w.findAll("[data-canvas] [data-box]")).toHaveLength(3);
  expect(crumb(w).text()).toContain("story"); // still inside the story's bundle
  await crumb(w).trigger("click");
  await flushPromises();
  (dialog()!.querySelector("[data-leave-go]") as HTMLButtonElement).click(); // the layer's own unsaved box is asked about
  await flushPromises();
  expect(router.currentRoute.value.params.name).toBe("story");
  expect(w.findAll("[data-canvas] [data-box]")).toHaveLength(23);
  expect(crumb(w).exists()).toBe(false);
});

test("the breadcrumb returns to the parent with its unsaved edits", async () => {
  withLayer();
  const { w, router } = await open();
  await w.get("[data-palette-type=gate]").trigger("click"); // an unsaved box in the story
  expect(w.findAll("[data-canvas] [data-box]")).toHaveLength(23);
  await w.get("[data-box][data-node=build_layers]").trigger("dblclick");
  await flushPromises();
  expect(dialog()).toBeNull(); // leaving for the bundle is not leaving the edits behind: nothing asked
  await crumb(w).trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.params.name).toBe("story");
  expect(w.get("h1").text()).toBe("story");
  expect(w.findAll("[data-canvas] [data-box]")).toHaveLength(23);
  expect(w.get("[data-dirty]").text()).toContain("Unsaved");
  expect(crumb(w).exists()).toBe(false);
  expect(router.currentRoute.value.query).toEqual({}); // the token is spent
});

test("a breadcrumb whose token is gone still goes back, and the parent loads from the server", async () => {
  withLayer();
  const { w, router } = await open();
  await w.get("[data-palette-type=gate]").trigger("click");
  await w.get("[data-box][data-node=build_layers]").trigger("dblclick");
  await flushPromises();
  sessionStorage.clear(); // expired
  await crumb(w).trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.params.name).toBe("story");
  expect(w.findAll("[data-canvas] [data-box]")).toHaveLength(22);
  expect(w.find("[data-dirty]").exists()).toBe(false);
  expect(w.find("[role=alert]").exists()).toBe(false);
  expect(api.getWorkflowDef.mock.calls.filter((c) => c[0] === "story")).toHaveLength(2); // loaded again from the server
});

test("a double-click on a bundle that names nothing that exists opens its fields, so the name can be fixed", async () => {
  const yaml = STORY_YAML.replace("workflow: layer", "workflow: gone");
  api.getWorkflowDef.mockResolvedValue(detail({ yaml }));
  const { w, router } = await open();
  await w.get("[data-box][data-node=build_layers]").trigger("dblclick");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/workflows/build/story");
  const modal = document.body.querySelector("[data-node-modal]")!;
  expect((modal.querySelector("select[data-input=workflow]") as HTMLSelectElement).value).toBe("gone");
  expect(modal.querySelector("[data-bundle-open]")).toBeNull();
});

test("a bundle child's problem does not mark the parent's box of the same name", async () => {
  api.getWorkflowDef.mockResolvedValue(detail({ problems: ["workflow layer: node survey: bad", "workflow story: node intake: worse"] }));
  const { w } = await open();
  expect(w.findAll("[data-problem-node]").map((b) => b.attributes("data-problem-node"))).toEqual(["intake"]); // the child's row names no box here
  expect(w.findAll("[data-problem-list] [data-problem]")).toHaveLength(2); // but it is still listed
  expect(w.get("[data-box][data-node=survey]").text()).not.toContain("bad"); // and the child's survey box is not this one's
});
