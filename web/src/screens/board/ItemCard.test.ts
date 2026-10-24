import { mount } from "@vue/test-utils";
import ItemCard from "./ItemCard.vue";
import { item } from "./fixtures";

test("a card shows its key, type, title and repo label", () => {
  const w = mount(ItemCard, { props: { item: item("AIR-7", { level: "epic", labels: ["repo:weyenk/airlie"] }), fresh: false } });
  expect(w.text()).toContain("AIR-7");
  expect(w.text()).toContain("epic");
  expect(w.text()).toContain("Title of AIR-7");
  expect(w.text()).toContain("repo:weyenk/airlie");
  expect(w.find("[data-fresh-note]").exists()).toBe(false);
});

test("a long title and repo are cut, keep their full text in a title attribute, and never wrap the key", () => {
  const longTitle = "x".repeat(200);
  const longRepo = "repo:" + "o".repeat(100) + "/name";
  const w = mount(ItemCard, { props: { item: item("AIR-123", { title: longTitle, labels: [longRepo] }), fresh: false } });
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
  const w = mount(ItemCard, { props: { item: item("AIR-17"), fresh: true } });
  expect(w.classes().join(" ")).toMatch(/ring-2/);
  expect(w.find("[data-fresh-note]").text()).toBe("updated just now");
});

test("a click or Enter opens it, and a drag carries its key", async () => {
  const w = mount(ItemCard, { props: { item: item("AIR-8"), fresh: false } });
  await w.trigger("click");
  await w.trigger("keydown", { key: "Enter" });
  expect(w.emitted("open")).toEqual([["AIR-8"], ["AIR-8"]]);
  const data: Record<string, string> = {};
  expect(w.attributes("draggable")).toBe("true");
  await w.trigger("dragstart", { dataTransfer: { setData: (k: string, v: string) => (data[k] = v), effectAllowed: "" } });
  expect(data["text/plain"]).toBe("AIR-8");
});
