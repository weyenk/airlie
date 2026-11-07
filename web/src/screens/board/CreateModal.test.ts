import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import type { Item } from "@/api/board";
import type { WorkflowDef } from "@/api/workflow_defs";
import { useCardDraftStore } from "@/stores/card_draft";
import CreateModal from "./CreateModal.vue";
import { item } from "@/test-utils/board-fixtures";

const api = vi.hoisted(() => ({
  listItems: vi.fn(),
  createItem: vi.fn(),
  patchItem: vi.fn(),
  moveItem: vi.fn(),
  addComment: vi.fn(),
  editComment: vi.fn(),
}));
vi.mock("@/api/board", () => api);
const wf = vi.hoisted(() => ({ repoNames: vi.fn() }));
vi.mock("@/api/workflows", () => wf);
const defs = vi.hoisted(() => ({ listWorkflowDefs: vi.fn() }));
vi.mock("@/api/workflow_defs", () => defs);

const DEFS: WorkflowDef[] = [
  { name: "story", type: "story", source: "file", problems: [], pr: null },
  { name: "bug-fix", type: "bug", source: "file", problems: [], pr: null },
  { name: "review-loop", type: null, source: "file", problems: [], pr: null, bundle: true },
  { name: "my-draft", type: "story", source: "draft", problems: [], pr: null },
];

const ITEMS: Item[] = [
  item("AIR-1", { level: "initiative", title: "The initiative" }),
  item("AIR-2", { level: "epic", title: "The epic", parent: "AIR-1" }),
  item("AIR-4", { level: "story", title: "The story", parent: "AIR-2", labels: ["repo:weyenk/airlie", "web"] }),
];

let router: ReturnType<typeof createRouter>;
async function open(props: Record<string, unknown> = {}, at = "/board") {
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/board/:key?", name: "board", component: { template: "<i/>" } },
      { path: "/workflows/build/:name", name: "builder", component: { template: "<i/>" } },
    ],
  });
  await router.push(at);
  const w = mount(CreateModal, { props, global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return w;
}
const q = <T extends Element = HTMLElement>(sel: string) => document.body.querySelector<T>(sel);
const type = (el: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) => {
  el.value = value;
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
};

beforeEach(() => {
  setActivePinia(createPinia());
  api.listItems.mockReset().mockResolvedValue(structuredClone(ITEMS));
  api.createItem.mockReset().mockImplementation(async (b: object) => ({ ...item("AIR-9"), ...b }));
  wf.repoNames.mockReset().mockResolvedValue(["weyenk/airlie", "weyenk/airlie-evals", "weyenk/openhand"]);
  defs.listWorkflowDefs.mockReset().mockResolvedValue(structuredClone(DEFS));
  sessionStorage.clear();
});
afterEach(() => {
  document.body.innerHTML = "";
});

test("offers the four levels as tiles, Story chosen, and the button names the type", async () => {
  await open();
  const tiles = [...document.body.querySelectorAll("[data-type]")];
  expect(tiles.map((t) => t.textContent?.trim())).toEqual(["Initiative", "Epic", "Story", "Subtask"]);
  expect(q("[data-type][aria-pressed=true]")!.textContent).toContain("Story");
  expect(q("[data-submit]")!.textContent).toBe("Create story");
  (tiles[1] as HTMLElement).click();
  await flushPromises();
  expect(q("[data-submit]")!.textContent).toBe("Create epic");
});

test("creates the item with the type, title, description, parent and repository label", async () => {
  const w = await open();
  type(q<HTMLInputElement>("input[aria-label=Title]")!, "A new story");
  type(q<HTMLSelectElement>("select[aria-label=Parent]")!, "AIR-2");
  type(q<HTMLInputElement>("input[aria-label=Repository]")!, "weyenk/openhand");
  type(q<HTMLTextAreaElement>("textarea[aria-label=Description]")!, "Why");
  (q("[data-submit]") as HTMLElement).click();
  await flushPromises();
  expect(api.createItem).toHaveBeenCalledWith({
    level: "story",
    title: "A new story",
    description: "Why",
    parent: "AIR-2",
    labels: ["repo:weyenk/openhand", "workflow:story"],
  });
  expect(w.emitted("created")![0][0]).toMatchObject({ key: "AIR-9" });
});

test("a title is required", async () => {
  await open();
  (q("[data-submit]") as HTMLElement).click();
  await flushPromises();
  expect(api.createItem).not.toHaveBeenCalled();
  expect(q("[role=alert]")!.textContent).toContain("title");
});

test("the parent choices are the level above, and a subtask must have one", async () => {
  await open();
  const options = (sel: string) => [...document.body.querySelectorAll(`${sel} option`)].map((o) => o.textContent?.trim());
  expect(options("select[aria-label=Parent]")).toEqual(["None", "AIR-2 The epic"]);
  (document.body.querySelectorAll("[data-type]")[3] as HTMLElement).click();
  await flushPromises();
  expect(options("select[aria-label=Parent]")).toEqual(["None", "AIR-4 The story"]);
  type(q<HTMLInputElement>("input[aria-label=Title]")!, "A subtask");
  (q("[data-submit]") as HTMLElement).click();
  await flushPromises();
  expect(api.createItem).not.toHaveBeenCalled();
  expect(q("[role=alert]")!.textContent).toContain("parent");
});

test("a child starts with its parent's type below it and its parent's repository", async () => {
  await open({ parent: ITEMS[2] });
  expect(q("[data-submit]")!.textContent).toBe("Create subtask");
  expect(q<HTMLSelectElement>("select[aria-label=Parent]")!.value).toBe("AIR-4");
  expect(q<HTMLInputElement>("input[aria-label=Repository]")!.value).toBe("weyenk/airlie");
  type(q<HTMLInputElement>("input[aria-label=Title]")!, "Child");
  (q("[data-submit]") as HTMLElement).click();
  await flushPromises();
  expect(api.createItem).toHaveBeenCalledWith({ level: "subtask", title: "Child", description: "", parent: "AIR-4", labels: ["repo:weyenk/airlie", "workflow:story"] });
});

test("a repository that is not owner/name is refused", async () => {
  await open();
  type(q<HTMLInputElement>("input[aria-label=Title]")!, "T");
  type(q<HTMLInputElement>("input[aria-label=Repository]")!, "nope");
  (q("[data-submit]") as HTMLElement).click();
  await flushPromises();
  expect(api.createItem).not.toHaveBeenCalled();
  expect(q("[role=alert]")!.textContent).toContain("owner/name");
});

test("the repository field completes a known name with Tab", async () => {
  await open();
  const input = q<HTMLInputElement>("input[aria-label=Repository]")!;
  type(input, "weyenk/air");
  await flushPromises();
  input.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
  await flushPromises();
  expect(input.value).toBe("weyenk/airlie");
});

test("Cancel and the close button ask to close", async () => {
  const w = await open();
  (q("[data-cancel]") as HTMLElement).click();
  (q("button[aria-label=Close]") as HTMLElement).click();
  await flushPromises();
  expect(w.emitted("close")).toHaveLength(2);
});

test("a refused create shows the server's words and keeps the form", async () => {
  api.createItem.mockRejectedValue(new Error("a story belongs under an epic"));
  const w = await open();
  type(q<HTMLInputElement>("input[aria-label=Title]")!, "T");
  (q("[data-submit]") as HTMLElement).click();
  await flushPromises();
  expect(q("[role=alert]")!.textContent).toContain("belongs under an epic");
  expect(w.emitted("created")).toBeUndefined();
  expect(q<HTMLInputElement>("input[aria-label=Title]")!.value).toBe("T");
});

const workflowSelect = () => q<HTMLSelectElement>("select[data-workflow]")!;

test("the Workflow select is required, starts on story, and lists files by type, then drafts, then Build new", async () => {
  await open();
  const select = workflowSelect();
  expect(select.required).toBe(true);
  expect(select.value).toBe("story");
  expect(q(`label[for="${select.id}"]`)!.textContent).toBe("Workflow");
  const groups = [...select.querySelectorAll("optgroup")].map((g) => [g.label, [...g.querySelectorAll("option")].map((o) => o.textContent?.trim())]);
  expect(groups).toEqual([
    ["story", ["story"]],
    ["bug", ["bug-fix"]],
    ["Drafts", ["my-draft (draft)"]],
  ]);
  const last = select.options[select.options.length - 1];
  expect([last.value, last.textContent?.trim()]).toEqual(["__new__", "＋ Build new…"]);
  expect([...select.options].map((o) => o.value)).not.toContain("review-loop");
});

test("the chosen workflow goes on the card as a workflow: label", async () => {
  await open();
  type(q<HTMLInputElement>("input[aria-label=Title]")!, "A bug");
  type(workflowSelect(), "bug-fix");
  (q("[data-submit]") as HTMLElement).click();
  await flushPromises();
  expect(api.createItem).toHaveBeenCalledWith({ level: "story", title: "A bug", description: "", parent: null, labels: ["workflow:bug-fix"] });
});

test("Build new holds what was typed and opens the builder with the card's token", async () => {
  await open();
  type(q<HTMLInputElement>("input[aria-label=Title]")!, "Half written");
  type(q<HTMLInputElement>("input[aria-label=Repository]")!, "weyenk/openhand");
  type(q<HTMLTextAreaElement>("textarea[aria-label=Description]")!, "Why");
  type(workflowSelect(), "__new__");
  await flushPromises();
  const route = router.currentRoute.value;
  expect(route.name).toBe("builder");
  expect(route.params.name).toBe("new");
  const token = String(route.query.card);
  expect(useCardDraftStore().take(token)).toEqual({
    level: "story",
    title: "Half written",
    description: "Why",
    parent: null,
    repo: "weyenk/openhand",
    workflow: "story",
  });
  expect(api.createItem).not.toHaveBeenCalled();
});

test("coming back with the token restores the form and the workflow the builder chose, and a create drops it", async () => {
  setActivePinia(createPinia());
  const drafts = useCardDraftStore();
  const token = drafts.hold({ level: "epic", title: "Kept", description: "Still here", parent: "AIR-1", repo: "weyenk/airlie", workflow: "story" });
  drafts.setWorkflow(token, "my-draft");
  await open({}, `/board?create=${token}`);
  expect(q<HTMLInputElement>("input[aria-label=Title]")!.value).toBe("Kept");
  expect(q<HTMLTextAreaElement>("textarea[aria-label=Description]")!.value).toBe("Still here");
  expect(q<HTMLSelectElement>("select[aria-label=Parent]")!.value).toBe("AIR-1");
  expect(q<HTMLInputElement>("input[aria-label=Repository]")!.value).toBe("weyenk/airlie");
  expect(q("[data-type][aria-pressed=true]")!.textContent).toContain("Epic");
  expect(workflowSelect().value).toBe("my-draft");
  (q("[data-submit]") as HTMLElement).click();
  await flushPromises();
  expect(api.createItem).toHaveBeenCalledWith({
    level: "epic",
    title: "Kept",
    description: "Still here",
    parent: "AIR-1",
    labels: ["repo:weyenk/airlie", "workflow:my-draft"],
  });
  expect(drafts.take(token)).toBeNull();
});

test("a workflow the list does not know yet is still shown as chosen", async () => {
  defs.listWorkflowDefs.mockRejectedValue(new Error("down"));
  await open();
  expect(workflowSelect().value).toBe("story");
});
