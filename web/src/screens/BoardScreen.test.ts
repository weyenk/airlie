import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import type { Item } from "@/api/board";
import BoardScreen from "./BoardScreen.vue";
import { DRAG_TYPE } from "./board/statuses";
import { item } from "./board/fixtures";

const api = vi.hoisted(() => ({
  listItems: vi.fn(),
  createItem: vi.fn(),
  patchItem: vi.fn(),
  moveItem: vi.fn(),
  addComment: vi.fn(),
  editComment: vi.fn(),
}));
vi.mock("@/api/board", () => api);

const ITEMS: Item[] = [
  item("AIR-1", { status: "todo", level: "epic" }),
  item("AIR-2", { status: "todo" }),
  item("AIR-3", { status: "doing" }),
  item("AIR-4", { status: "done", level: "initiative" }),
];

const ours = (key: string) => ({ dataTransfer: { types: [DRAG_TYPE], getData: () => key } });

async function open() {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/board/:key?", name: "board", component: { template: "<i/>" } }] });
  await router.push("/board");
  const w = mount(BoardScreen, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return { w, router };
}

beforeEach(() => {
  setActivePinia(createPinia());
  api.listItems.mockReset().mockResolvedValue(structuredClone(ITEMS));
  api.moveItem.mockReset().mockImplementation(async (key: string, status: Item["status"]) => item(key, { status, updated_at: 5 }));
});
afterEach(() => {
  document.body.innerHTML = "";
});

test("six columns in status order, each with its cards", async () => {
  const { w } = await open();
  const cols = w.findAll("section[aria-label]");
  expect(cols.map((c) => c.attributes("aria-label"))).toEqual(["To do", "Approved", "Doing", "Review", "Blocked", "Done"]);
  expect(cols[0].findAll("[data-item]").map((c) => c.attributes("data-item"))).toEqual(["AIR-1", "AIR-2"]);
  expect(cols[1].text()).toContain("Nothing here");
  expect(w.find("[data-columns]").exists()).toBe(true);
});

test("the Type filter narrows the cards to one level", async () => {
  const { w } = await open();
  await w.find("[data-type-filter]").trigger("keydown", { key: "Enter" });
  await flushPromises();
  const options = [...document.body.querySelectorAll("[role=menuitemradio]")].map((o) => o.textContent?.trim());
  expect(options).toEqual(["All", "Initiative", "Epic", "Story", "Subtask"]);
  (document.body.querySelectorAll("[role=menuitemradio]")[2] as HTMLElement).click();
  await flushPromises();
  expect(w.findAll("[data-item]").map((c) => c.attributes("data-item"))).toEqual(["AIR-1"]);
  expect(w.find("[data-count]").text()).toBe("1");
});

test("a drop posts the move with the status and the card it lands above", async () => {
  const { w } = await open();
  const doing = w.findAll("section[aria-label]")[2];
  await doing.find("[data-item]").trigger("drop", ours("AIR-1"));
  expect(api.moveItem).toHaveBeenCalledWith("AIR-1", "doing", "AIR-3");
  await doing.find("[data-list]").trigger("drop", ours("AIR-2"));
  expect(api.moveItem).toHaveBeenLastCalledWith("AIR-2", "doing", null);
});

test("a card click goes to its own address", async () => {
  const { w, router } = await open();
  await w.find("[data-item=AIR-3]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/board/AIR-3");
});

test("Create goes to ?create=1, where the create modal will open", async () => {
  const { w, router } = await open();
  await w.find("[data-create]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.query.create).toBe("1");
});

test("the phone's pills show one column, Doing first, and choosing another swaps it", async () => {
  const { w } = await open();
  const shown = () => w.findAll("section[aria-label]").filter((c) => !c.classes().includes("max-[640px]:hidden")).map((c) => c.attributes("aria-label"));
  expect(shown()).toEqual(["Doing"]);
  await w.findAll("[data-pills] button")[0].trigger("click");
  expect(shown()).toEqual(["To do"]);
  expect(w.find("[data-pills]").text()).toContain("To do 2");
});

test("the columns end 24px above the window: the page is the window less the header, with a 24px buffer", async () => {
  const { w } = await open();
  expect(w.find("[data-board]").classes().join(" ")).toContain("h-[calc(100dvh-var(--header-h))]");
  expect(w.find("[data-board]").classes()).toContain("pb-6");
  expect(w.find("[data-columns]").classes()).toContain("min-h-80"); // board.html's min-height: 320px
});
