import { mount } from "@vue/test-utils";
import { CATALOG } from "@/test-utils/catalog";
import CatalogPicker from "./CatalogPicker.vue";

const { checks } = CATALOG;
const mountPicker = (value = "") => mount(CatalogPicker, { props: { checks, value } });
const listed = (w: ReturnType<typeof mountPicker>) => w.findAll("[data-check]").map((c) => c.attributes("data-check"));

test("every check is listed by its title, with its name, what it reads and writes, and its args", () => {
  const w = mountPicker();
  expect(w.findAll("[data-check]")).toHaveLength(checks.length);
  const costed = w.get("[data-check='story.costed']");
  expect(costed.text()).toContain("Record what the stages named (args.stages) cost against their budgets");
  expect(costed.text()).toContain("story.costed");
  expect(costed.get("[data-check-inputs]").text()).toContain("card");
  expect(costed.get("[data-check-args]").text()).toContain("stages");
  expect(w.get("[data-check='story.base'] [data-check-outputs]").text()).toContain("base");
});

test("the search narrows the list by title or name", async () => {
  const w = mountPicker();
  await w.get("[data-catalog-search]").setValue("stacked draft");
  expect(listed(w)).toEqual(["story.publish"]);
  await w.get("[data-catalog-search]").setValue("STORY.BASE");
  expect(listed(w)).toEqual(["story.base"]);
  await w.get("[data-catalog-search]").setValue("no such check");
  expect(w.find("[data-catalog-none]").exists()).toBe(true);
});

test("a click picks the check; the one picked is marked", async () => {
  const w = mountPicker("story.base");
  expect(w.get("[data-check='story.base']").attributes("aria-pressed")).toBe("true");
  expect(w.get("[data-check='story.intake']").attributes("aria-pressed")).toBe("false");
  await w.get("[data-check='story.request']").trigger("click");
  expect(w.emitted("pick")).toEqual([[checks.find((c) => c.name === "story.request")]]);
});
