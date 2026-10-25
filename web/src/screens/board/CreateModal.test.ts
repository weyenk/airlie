import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import type { Item } from "@/api/board";
import CreateModal from "./CreateModal.vue";
import { item } from "./fixtures";

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

const ITEMS: Item[] = [
  item("AIR-1", { level: "initiative", title: "The initiative" }),
  item("AIR-2", { level: "epic", title: "The epic", parent: "AIR-1" }),
  item("AIR-4", { level: "story", title: "The story", parent: "AIR-2", labels: ["repo:weyenk/airlie", "web"] }),
];

async function open(props: Record<string, unknown> = {}) {
  const w = mount(CreateModal, { props, attachTo: document.body });
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
    labels: ["repo:weyenk/openhand"],
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
  expect(api.createItem).toHaveBeenCalledWith({ level: "subtask", title: "Child", description: "", parent: "AIR-4", labels: ["repo:weyenk/airlie"] });
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
