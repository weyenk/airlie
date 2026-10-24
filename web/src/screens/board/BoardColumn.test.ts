import { mount } from "@vue/test-utils";
import BoardColumn from "./BoardColumn.vue";
import { item } from "./fixtures";

const make = (items = [item("AIR-1"), item("AIR-2"), item("AIR-3")], extra: Record<string, unknown> = {}) =>
  mount(BoardColumn, { props: { status: "todo", items, changed: new Set<string>(), active: true, ...extra } });
const drop = (target: { trigger(e: string, o?: object): Promise<void> }, key: string) =>
  target.trigger("drop", { dataTransfer: { getData: () => key } });

test("a column is a region named for its status, with its count and its cards in order", () => {
  const w = make();
  expect(w.attributes("aria-label")).toBe("To do");
  expect(w.find("[data-count]").text()).toBe("3");
  expect(w.findAll("[data-item]").map((c) => c.attributes("data-item"))).toEqual(["AIR-1", "AIR-2", "AIR-3"]);
});

test("an empty column says Nothing here", () => {
  const w = make([]);
  expect(w.text()).toContain("Nothing here");
  expect(w.find("[data-count]").text()).toBe("0");
});

test("a card dropped on another card moves above it", async () => {
  const w = make();
  await drop(w.findAll("[data-item]")[1], "AIR-9");
  expect(w.emitted("move")).toEqual([["AIR-9", "todo", "AIR-2"]]);
});

test("a card dropped below the cards moves to the bottom", async () => {
  const w = make();
  await drop(w.find("[data-list]"), "AIR-9");
  expect(w.emitted("move")).toEqual([["AIR-9", "todo", null]]);
});

test("a card dropped on itself moves nowhere", async () => {
  const w = make();
  await drop(w.findAll("[data-item]")[0], "AIR-1");
  expect(w.emitted("move")).toBeUndefined();
});

test("dragging over shows where the card lands, leaving clears it", async () => {
  const w = make();
  await w.findAll("[data-item]")[1].trigger("dragover");
  const marks = w.findAll("[data-drop]");
  expect(marks).toHaveLength(1);
  expect(marks[0].element.nextElementSibling?.getAttribute("data-item")).toBe("AIR-2");
  await w.trigger("dragleave");
  expect(w.find("[data-drop]").exists()).toBe(false);
});

test("a card that opens or rings is passed on", async () => {
  const w = make(undefined, { changed: new Set(["AIR-3"]) });
  expect(w.findAll("[data-fresh-note]")).toHaveLength(1);
  await w.findAll("[data-item]")[0].trigger("click");
  expect(w.emitted("open")).toEqual([["AIR-1"]]);
});

test("off the phone's chosen status the column hides below 640px", () => {
  expect(make(undefined, { active: false }).classes()).toContain("max-[640px]:hidden");
  expect(make().classes()).not.toContain("max-[640px]:hidden");
});
