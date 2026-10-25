import { mount } from "@vue/test-utils";
import BoardColumn from "./BoardColumn.vue";
import { boardRouter, item } from "./fixtures";
import { DRAG_TYPE } from "./statuses";

const make = (items = [item("AIR-1"), item("AIR-2"), item("AIR-3")], extra: Record<string, unknown> = {}) =>
  mount(BoardColumn, { props: { status: "todo", items, changed: new Set<string>(), active: true, ...extra }, global: { plugins: [boardRouter()] } });
type Target = { trigger(e: string, o?: object): Promise<void> };
const ours = (key = "AIR-9") => ({ dataTransfer: { types: [DRAG_TYPE], getData: (t: string) => (t === DRAG_TYPE ? key : "") } });
const drop = (target: Target, key = "AIR-9") => target.trigger("drop", ours(key));
const over = (target: Target) => target.trigger("dragover", ours());

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
  await drop(w.findAll("[data-item]")[1]);
  expect(w.emitted("move")).toEqual([["AIR-9", "todo", "AIR-2"]]);
});

test("a card dropped below the cards, on the list or the strip under the last one, moves to the bottom", async () => {
  const w = make();
  await drop(w.find("[data-list]"));
  await drop(w.find("[data-tail]"));
  expect(w.emitted("move")).toEqual([
    ["AIR-9", "todo", null],
    ["AIR-9", "todo", null],
  ]);
});

test("a card dropped on itself moves nowhere", async () => {
  const w = make();
  await drop(w.findAll("[data-item]")[0], "AIR-1");
  expect(w.emitted("move")).toBeUndefined();
});

test("a drop that leaves the card where it is sends nothing", async () => {
  const w = make();
  await drop(w.findAll("[data-item]")[1], "AIR-1"); // AIR-1 above AIR-2: it already is
  await drop(w.find("[data-list]"), "AIR-3"); // AIR-3 to the bottom: it already is
  expect(w.emitted("move")).toBeUndefined();
  await drop(w.findAll("[data-item]")[2], "AIR-1"); // AIR-1 above AIR-3: a real move
  expect(w.emitted("move")).toEqual([["AIR-1", "todo", "AIR-3"]]);
});

test("dragging over shows where the card lands, leaving clears it", async () => {
  const w = make();
  await over(w.findAll("[data-item]")[1]);
  const marks = w.findAll("[data-drop]");
  expect(marks).toHaveLength(1);
  expect(marks[0].element.nextElementSibling?.getAttribute("data-item")).toBe("AIR-2");
  await w.trigger("dragleave");
  expect(w.find("[data-drop]").exists()).toBe(false);
});

test("the landing mark takes no pointer", async () => {
  const w = make();
  await over(w.findAll("[data-item]")[1]);
  expect(w.find("[data-drop]").classes()).toContain("pointer-events-none");
});

test("over the gap or the mark the list is what is hit: the place stays, and a drop there lands where the mark shows", async () => {
  const w = make();
  await over(w.findAll("[data-item]")[1]);
  await over(w.find("[data-list]")); // the real target over the mark's box and the 8px gaps
  expect(w.find("[data-drop]").element.nextElementSibling?.getAttribute("data-item")).toBe("AIR-2"); // not the bottom
  await drop(w.find("[data-list]"));
  expect(w.emitted("move")).toEqual([["AIR-9", "todo", "AIR-2"]]);
});

test("the strip below the last card is the bottom: the mark goes there and a drop sends null", async () => {
  const w = make();
  await over(w.findAll("[data-item]")[1]);
  await over(w.find("[data-tail]"));
  expect(w.find("[data-drop]").element.nextElementSibling?.getAttribute("data-tail")).toBe("");
  await drop(w.find("[data-tail]"));
  expect(w.emitted("move")).toEqual([["AIR-9", "todo", null]]);
});

test("a drag entering through the header, padding or gap resolves by the pointer's height, not to the bottom", async () => {
  const w = make();
  const cards = w.findAll("[data-item]");
  // card n spans y = 100n .. 100n+80 (middles 100n+40)
  cards.forEach((c, n) => {
    c.element.getBoundingClientRect = () => ({ top: 100 * n, height: 80 }) as DOMRect;
  });
  const at = (y: number) => ({ dataTransfer: { types: [DRAG_TYPE], getData: () => "AIR-9" }, clientY: y });
  const markAbove = () => w.find("[data-drop]").element.nextElementSibling?.getAttribute("data-item") ?? "bottom";

  await w.find("[data-count]").trigger("dragover", at(-30)); // the header, above every card
  expect(markAbove()).toBe("AIR-1");
  await w.trigger("dragleave");
  await w.trigger("dragover", at(150)); // the section's padding, past AIR-2's middle (140)
  expect(markAbove()).toBe("AIR-3");
  await w.trigger("dragleave");
  await w.find("[data-list]").trigger("dragover", at(130)); // the gap, before AIR-2's middle (140)
  expect(markAbove()).toBe("AIR-2");
  await w.find("[data-list]").trigger("dragover", at(260)); // later gap hovers keep it
  expect(markAbove()).toBe("AIR-2");
  await w.trigger("dragleave");
  await w.trigger("dragover", at(999)); // below every middle: an explicit bottom
  expect(markAbove()).toBe("bottom");
});

test("a drag that is not one of the board's cards is ignored", async () => {
  const w = make();
  const files = { dataTransfer: { types: ["Files"], getData: () => "AIR-9" } };
  await w.findAll("[data-item]")[1].trigger("dragover", files);
  expect(w.find("[data-drop]").exists()).toBe(false);
  await w.findAll("[data-item]")[1].trigger("drop", files);
  expect(w.emitted("move")).toBeUndefined();
});

test("a card that rings shows its note", () => {
  const w = make(undefined, { changed: new Set(["AIR-3"]) });
  expect(w.findAll("[data-fresh-note]")).toHaveLength(1);
});

test("off the phone's chosen status the column hides below 640px", () => {
  expect(make(undefined, { active: false }).classes()).toContain("max-[640px]:hidden");
  expect(make().classes()).not.toContain("max-[640px]:hidden");
});
