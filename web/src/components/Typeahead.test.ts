import { mount } from "@vue/test-utils";
import { flushPromises } from "@vue/test-utils";
import Typeahead from "./Typeahead.vue";

const source = ["weyenk/airlie", "weyenk/airlie-evals", "weyenk/openhand"];
const make = (extra: Record<string, unknown> = {}) => mount(Typeahead, { props: { modelValue: "", source, ...extra } });

test("typeahead completes the top match on Tab", async () => {
  const w = make();
  await w.find("input").setValue("weyenk/air");
  expect(w.findAll("[data-suggestion]").map((s) => s.text())).toEqual(["weyenk/airlie", "weyenk/airlie-evals"]);
  expect(w.find("[data-ghost]").text()).toBe("lie");
  await w.find("input").trigger("keydown", { key: "Tab" });
  expect(w.emitted("update:modelValue")!.at(-1)).toEqual(["weyenk/airlie"]);
});
test("prefix matches come first, then substrings, case-insensitive, at most three", async () => {
  const w = make({ source: ["a/xAIR", "AIRb", "airc", "aird", "air"] });
  await w.find("input").setValue("air");
  expect(w.findAll("[data-suggestion]").map((s) => s.text())).toEqual(["AIRb", "airc", "aird"]);
});
test("the typed part is bold", async () => {
  const w = make();
  await w.find("input").setValue("weyenk/air");
  expect(w.find("[data-suggestion] b").text()).toBe("weyenk/air");
});
test("arrows move, Enter accepts the moved-to match, Escape closes", async () => {
  const w = make();
  const input = w.find("input");
  await input.setValue("weyenk/air");
  await input.trigger("keydown", { key: "ArrowDown" });
  await input.trigger("keydown", { key: "Enter" });
  expect(w.emitted("update:modelValue")!.at(-1)).toEqual(["weyenk/airlie-evals"]);
  await input.setValue("weyenk/air");
  expect(w.findAll("[data-suggestion]").length).toBe(2);
  await input.trigger("keydown", { key: "Escape" });
  expect(w.findAll("[data-suggestion]").length).toBe(0);
});
test("a function source is asked, and its suggestions show with their label and meta", async () => {
  const fetcher = vi.fn(async (q: string) => [{ value: `${q}-1`, label: "One", meta: "3 stories" }]);
  const w = make({ source: fetcher });
  await w.find("input").setValue("x");
  await flushPromises();
  expect(fetcher).toHaveBeenCalledWith("x");
  expect(w.find("[data-suggestion]").text()).toContain("One");
  expect(w.find("[data-suggestion]").text()).toContain("3 stories");
  await w.find("input").trigger("keydown", { key: "Enter" });
  expect(w.emitted("update:modelValue")!.at(-1)).toEqual(["x-1"]);
});
