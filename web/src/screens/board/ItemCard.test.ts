import { flushPromises, mount } from "@vue/test-utils";
import ItemCard from "./ItemCard.vue";
import { boardRouter, item } from "@/test-utils/board-fixtures";
import { DRAG_TYPE } from "./statuses";

const make = (props: { item: ReturnType<typeof item>; fresh: boolean }, router = boardRouter()) =>
  mount(ItemCard, { props, global: { plugins: [router] } });

test("a card shows its key, type, title and repo label", () => {
  const w = make({ item: item("AIR-7", { level: "epic", labels: ["repo:weyenk/airlie"] }), fresh: false });
  expect(w.text()).toContain("AIR-7");
  expect(w.text()).toContain("epic");
  expect(w.text()).toContain("Title of AIR-7");
  expect(w.text()).toContain("repo:weyenk/airlie");
  expect(w.find("[data-fresh-note]").exists()).toBe(false);
});

test("a long title and repo are cut, keep their full text in a title attribute, and never wrap the key", () => {
  const longTitle = "x".repeat(200);
  const longRepo = "repo:" + "o".repeat(100) + "/name";
  const w = make({ item: item("AIR-123", { title: longTitle, labels: [longRepo] }), fresh: false });
  const title = w.find("[data-title]");
  expect(title.text()).toBe(longTitle);
  expect(title.attributes("title")).toBe(longTitle);
  expect(title.classes()).toContain("line-clamp-3");
  expect(title.classes().join(" ")).toMatch(/break-words|wrap-anywhere|break-all/);
  const label = w.find("[data-label]");
  expect(label.attributes("title")).toBe(longRepo);
  expect(label.classes()).toContain("truncate");
  expect(w.find("[data-key]").classes()).toContain("whitespace-nowrap");
  expect(w.classes()).toContain("min-w-0");
});

test("a card whose key someone else changed rings and says so", () => {
  const w = make({ item: item("AIR-17"), fresh: true });
  expect(w.classes().join(" ")).toMatch(/ring-2/);
  expect(w.find("[data-fresh-note]").text()).toBe("updated just now");
});

test("a card is a real link to its own address, named by key and title", () => {
  const w = make({ item: item("AIR-8", { title: "Fix it" }), fresh: false });
  expect(w.element.tagName).toBe("A");
  expect(w.attributes("href")).toBe("/board/AIR-8");
  expect(w.attributes("aria-label")).toBe("AIR-8 Fix it");
  expect(w.attributes("draggable")).toBe("true");
});

test("following the link (a click, which Enter on a link makes) goes to the card", async () => {
  const router = boardRouter();
  const w = make({ item: item("AIR-8"), fresh: false }, router);
  await w.trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/board/AIR-8");
});

test("a drag carries the key under the board's own type", async () => {
  const w = make({ item: item("AIR-8"), fresh: false });
  const data: Record<string, string> = {};
  await w.trigger("dragstart", { dataTransfer: { setData: (k: string, v: string) => (data[k] = v), effectAllowed: "" } });
  expect(data[DRAG_TYPE]).toBe("AIR-8");
  expect(DRAG_TYPE).toBe("application/x-airlie-item");
});
