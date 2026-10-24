import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import type { Item } from "@/api/board";
import type { Flow } from "@/api/workflows";
import { useBoardStore } from "@/stores/board";
import { useSessionStore } from "@/stores/session";
import CardModal from "./CardModal.vue";
import { item } from "./fixtures";

const api = vi.hoisted(() => ({
  listItems: vi.fn(),
  getItem: vi.fn(),
  createItem: vi.fn(),
  patchItem: vi.fn(),
  moveItem: vi.fn(),
  addComment: vi.fn(),
  editComment: vi.fn(),
}));
vi.mock("@/api/board", () => api);
const wf = vi.hoisted(() => ({ storyFlow: vi.fn(), repoNames: vi.fn() }));
vi.mock("@/api/workflows", () => wf);

const STORY = item("AIR-4", {
  title: "Board: panel width",
  status: "approved",
  parent: "AIR-2",
  description: "The panel must be **wide**",
  labels: ["repo:weyenk/airlie", "web"],
  comments: [
    { id: 1, author: "noah", text: "first note", at: 100 },
    { id: 2, author: "airlie", text: "a bot note", at: 200 },
  ],
  created_at: 1000,
  updated_at: 2000,
});
const ITEMS: Item[] = [
  item("AIR-2", { level: "epic", title: "The epic" }),
  STORY,
  item("AIR-5", { level: "subtask", parent: "AIR-4", title: "A child", status: "doing" }),
];
const FLOW: Flow = {
  key: "AIR-4",
  title: "Board: panel width",
  summary: null,
  stage: "layers",
  blocked: null,
  acceptance: [{ id: "AC1", given: "an open panel", when: "the handle is dragged", then: "the width follows" }] as never,
  nodes: [
    { id: "intake", label: "Intake", kind: "stage", status: "passed", jobs: [] },
    { id: "layer-01", label: "Layer 01", kind: "layer", status: "passed", title: "Resizable panel", jobs: [], attempts: 1 },
    { id: "layer-02", label: "Layer 02", kind: "layer", status: "running", title: "Full screen", jobs: [], attempts: 1 },
  ],
};

async function open(key = "AIR-4") {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/board/:key?", name: "board", component: { template: "<i/>" } },
      { path: "/workflows/:key/:node?/:job?", name: "story", component: { template: "<i/>" } },
    ],
  });
  await router.push(`/board/${key}`);
  const w = mount(CardModal, { props: { itemKey: key }, global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return { w, router };
}
const body = () => document.body;
const q = <T extends Element = HTMLElement>(sel: string) => body().querySelector<T>(sel);

beforeEach(() => {
  setActivePinia(createPinia());
  useSessionStore().me = { name: "noah", role: "admin", via: "key" };
  api.listItems.mockReset().mockResolvedValue(structuredClone(ITEMS));
  api.getItem.mockReset().mockImplementation(async (k: string) => structuredClone(ITEMS.find((i) => i.key === k)));
  api.patchItem.mockReset().mockImplementation(async (k: string, b: Partial<Item>) => ({ ...structuredClone(STORY), key: k, ...b, updated_at: 3000 }));
  api.moveItem.mockReset().mockImplementation(async (k: string, status: Item["status"]) => ({ ...structuredClone(STORY), key: k, status, updated_at: 3000 }));
  api.addComment.mockReset().mockResolvedValue({ ...STORY, updated_at: 3000 });
  api.editComment.mockReset().mockResolvedValue({ ...STORY, updated_at: 3000 });
  wf.storyFlow.mockReset().mockResolvedValue(structuredClone(FLOW));
  wf.repoNames.mockReset().mockResolvedValue(["weyenk/airlie", "weyenk/openhand"]);
});
afterEach(() => {
  document.body.innerHTML = "";
});

test("shows the key, the title, the status, the type, the parent link and the repository", async () => {
  await open();
  const dialog = q("[role=dialog]")!;
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  expect(dialog.textContent).toContain("AIR-4");
  expect(q("h2")!.textContent).toBe("Board: panel width");
  expect(dialog.textContent).toContain("Approved");
  expect(dialog.textContent).toContain("story");
  const parent = q<HTMLAnchorElement>("a[href='/board/AIR-2']")!;
  expect(parent.textContent).toContain("AIR-2");
  expect(dialog.textContent).toContain("weyenk/airlie");
});

test("Move to offers every status but the current one and moves the card", async () => {
  await open();
  const moves = [...body().querySelectorAll<HTMLButtonElement>("[data-move] button")];
  expect(moves.map((b) => b.textContent?.trim())).toEqual(["To do", "Doing", "Review", "Blocked", "Done"]);
  moves[1].click();
  await flushPromises();
  expect(api.moveItem).toHaveBeenCalledWith("AIR-4", "doing", null);
});

test("the description shows as Markdown", async () => {
  await open();
  expect(q("[data-description] strong")!.textContent).toBe("wide");
});

test("editing the description sends it; Escape in the editor discards it and the modal stays", async () => {
  const { w } = await open();
  q("[data-description] [data-display]")!.click();
  await flushPromises();
  const area = q<HTMLTextAreaElement>("textarea[aria-label=Description]")!;
  expect(area.value).toBe("The panel must be **wide**");
  area.value = "changed";
  area.dispatchEvent(new Event("input"));
  area.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  await flushPromises();
  expect(api.patchItem).not.toHaveBeenCalled();
  expect(q("textarea[aria-label=Description]")).toBeNull();
  expect(q("[role=dialog]")).not.toBeNull();
  expect(w.emitted("close")).toBeUndefined();
});

test("an empty description shows the prompt", async () => {
  api.listItems.mockResolvedValue([{ ...STORY, description: "" }]);
  await open();
  expect(q("[data-description]")!.textContent).toContain("Add a more detailed description");
});

test("the repository saves as a repo: label and keeps the other labels", async () => {
  await open();
  q("[data-repo] [data-display]")!.click();
  await flushPromises();
  const input = q<HTMLInputElement>("input[aria-label=Repository]")!;
  expect(input.value).toBe("weyenk/airlie");
  input.value = "weyenk/openhand";
  input.dispatchEvent(new Event("input"));
  input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await flushPromises();
  expect(api.patchItem).toHaveBeenCalledWith("AIR-4", { labels: ["repo:weyenk/openhand", "web"] });
  expect(q("[data-repo]")!.textContent).toContain("weyenk/openhand");
});

test("a repository that is not owner/name is refused, nothing is sent, and the field keeps what was typed", async () => {
  await open();
  q("[data-repo] [data-display]")!.click();
  await flushPromises();
  const input = q<HTMLInputElement>("input[aria-label=Repository]")!;
  input.value = "not-a-repo";
  input.dispatchEvent(new Event("input"));
  input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await flushPromises();
  expect(api.patchItem).not.toHaveBeenCalled();
  expect(q("[data-repo] [role=alert]")!.textContent).toContain("owner/name");
  expect(q<HTMLInputElement>("input[aria-label=Repository]")!.value).toBe("not-a-repo");
});

test("a repository of only dots is refused", async () => {
  await open();
  q("[data-repo] [data-display]")!.click();
  await flushPromises();
  const input = q<HTMLInputElement>("input[aria-label=Repository]")!;
  input.value = "../..";
  input.dispatchEvent(new Event("input"));
  input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await flushPromises();
  expect(api.patchItem).not.toHaveBeenCalled();
});

test("a repository can be added to a card with none", async () => {
  api.listItems.mockResolvedValue([{ ...STORY, labels: ["web"] }]);
  await open();
  expect(q("[data-repo]")!.textContent).toContain("Add a repository");
  q("[data-repo] [data-display]")!.click();
  await flushPromises();
  const input = q<HTMLInputElement>("input[aria-label=Repository]")!;
  input.value = "weyenk/OpenHand";
  input.dispatchEvent(new Event("input"));
  input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await flushPromises();
  expect(api.patchItem).toHaveBeenCalledWith("AIR-4", { labels: ["repo:weyenk/OpenHand", "web"] });
});

test("the title edits in place and Enter saves it", async () => {
  await open();
  q("[data-title] [data-display]")!.click();
  await flushPromises();
  const input = q<HTMLInputElement>("input[aria-label=Title]")!;
  expect(input.value).toBe("Board: panel width");
  input.value = "Wider";
  input.dispatchEvent(new Event("input"));
  input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await flushPromises();
  expect(api.patchItem).toHaveBeenCalledWith("AIR-4", { title: "Wider" });
});

test("acceptance criteria show Given, When and Then, each in its own token colour", async () => {
  await open();
  const section = q("[data-acceptance]")!;
  expect(section.textContent).toContain("Acceptance criteria");
  expect(section.textContent).toContain("AC1");
  const kws = [...section.querySelectorAll("[data-kw]")];
  expect(kws.map((k) => k.textContent)).toEqual(["Given", "when", "then"]);
  const colours = kws.map((k) => k.className.split(" ").find((c) => /^text-(info|warn|ok)-foreground$/.test(c)));
  expect(colours).toEqual(["text-info-foreground", "text-warn-foreground", "text-ok-foreground"]);
});

test("the workflow lists each layer with its status and Open pipeline links to the story", async () => {
  await open();
  const section = q("[data-workflow]")!;
  expect(section.textContent).toContain("Layer 01");
  expect(section.textContent).toContain("Resizable panel");
  expect(section.textContent).toContain("passed");
  expect(section.textContent).toContain("running");
  expect(section.textContent).toContain("layer 02 of 2");
  const link = [...section.querySelectorAll("a")].find((a) => a.textContent?.includes("Open pipeline"))!;
  expect(link.getAttribute("href")).toBe("/workflows/AIR-4");
});

test("a card with no story behind it has no acceptance criteria or workflow", async () => {
  wf.storyFlow.mockRejectedValue(Object.assign(new Error("No story"), { status: 404 }));
  await open();
  expect(q("[data-acceptance]")).toBeNull();
  expect(q("[data-workflow]")).toBeNull();
});

test("only a story asks for a flow", async () => {
  await open("AIR-5");
  expect(wf.storyFlow).not.toHaveBeenCalled();
});

test("subtasks list the children and Add child asks for Create with this card as the parent", async () => {
  const { w } = await open();
  const section = q("[data-children]")!;
  expect(section.textContent).toContain("Subtasks");
  expect(section.textContent).toContain("AIR-5");
  expect(section.textContent).toContain("A child");
  [...section.querySelectorAll("button")].find((b) => b.textContent?.includes("Add child"))!.click();
  expect(w.emitted("add-child")).toEqual([[STORY]]);
});

test("a subtask has no children section", async () => {
  await open("AIR-5");
  expect(q("[data-children]")).toBeNull();
});

test("comments: your own can be edited, someone else's cannot, and the footer says who made the card", async () => {
  await open();
  const rows = [...body().querySelectorAll("[data-comment]")];
  expect(rows).toHaveLength(2);
  expect(rows[0].querySelector("[data-edit]")).not.toBeNull();
  expect(rows[1].querySelector("[data-edit]")).toBeNull();
  expect(q("[data-foot]")!.textContent).toContain("Created");
  expect(q("[data-foot]")!.textContent).toContain("by noah");
});

test("saving an edited comment calls the comment route with its id", async () => {
  await open();
  (q("[data-comment] [data-edit]") as HTMLElement).click();
  await flushPromises();
  const area = q<HTMLTextAreaElement>("[data-comment] textarea")!;
  area.value = "edited note";
  area.dispatchEvent(new Event("input"));
  (q("[data-comment] [data-save]") as HTMLElement).click();
  await flushPromises();
  expect(api.editComment).toHaveBeenCalledWith("AIR-4", 1, "edited note");
});

test("the close button asks to close", async () => {
  const { w } = await open();
  (q("button[aria-label=Close]") as HTMLElement).click();
  await flushPromises();
  expect(w.emitted("close")).toHaveLength(1);
});

test("a key that is not on the board says so", async () => {
  api.listItems.mockResolvedValue([]);
  api.getItem.mockRejectedValue(Object.assign(new Error("No such item"), { status: 404 }));
  await open("AIR-99");
  expect(q("[role=dialog]")!.textContent).toContain("No such item");
});

test("the modal fills the screen below 1024px and is 760px wide above it", async () => {
  await open();
  const cls = q("[role=dialog]")!.className;
  expect(cls).toContain("w-[760px]");
  expect(cls).toContain("max-[1023px]:fixed");
  expect(cls).toContain("max-[1023px]:inset-0");
});

test("on a phone, Acceptance criteria and Workflow start folded", async () => {
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("639"), media: query, addEventListener() {}, removeEventListener() {} }));
  await open();
  expect(q("[data-acceptance] [data-state]")!.getAttribute("data-state")).toBe("closed");
  expect(q("[data-workflow] [data-state]")!.getAttribute("data-state")).toBe("closed");
  vi.unstubAllGlobals();
});

test("on a desktop they start open", async () => {
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }));
  await open();
  expect(q("[data-acceptance] [data-state]")!.getAttribute("data-state")).toBe("open");
  vi.unstubAllGlobals();
});

test("an open editor is saved when the card is closed by a click (focus leaves it first)", async () => {
  const { w } = await open();
  q("[data-description] [data-display]")!.click();
  await flushPromises();
  const area = q<HTMLTextAreaElement>("textarea[aria-label=Description]")!;
  area.focus();
  area.value = "typed then closed";
  area.dispatchEvent(new Event("input"));
  (q("button[aria-label=Close]") as HTMLElement).click(); // a real click would also move focus; the modal blurs it
  await flushPromises();
  expect(api.patchItem).toHaveBeenCalledWith("AIR-4", { description: "typed then closed" });
  expect(w.emitted("close")).toHaveLength(1);
});

test("a card that was on the board and is gone says it was deleted", async () => {
  await open();
  api.listItems.mockResolvedValue(ITEMS.filter((i) => i.key !== "AIR-4"));
  await useBoardStore().refresh();
  await flushPromises();
  expect(q("[role=dialog]")!.textContent).toContain("This item was deleted.");
  expect(q("[data-title]")).toBeNull();
});
