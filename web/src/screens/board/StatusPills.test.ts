import { mount } from "@vue/test-utils";
import StatusPills from "./StatusPills.vue";

const counts = { todo: 2, approved: 1, doing: 2, review: 2, blocked: 1, done: 24 };

test("a pill per status with its count, the chosen one marked", () => {
  const w = mount(StatusPills, { props: { counts, modelValue: "doing" } });
  expect(w.findAll("button").map((b) => b.text())).toEqual(["To do 2", "Approved 1", "Doing 2", "Review 2", "Blocked 1", "Done 24"]);
  expect(w.findAll("button").map((b) => b.attributes("aria-pressed"))).toEqual(["false", "false", "true", "false", "false", "false"]);
});

test("choosing a pill says which", async () => {
  const w = mount(StatusPills, { props: { counts, modelValue: "doing" } });
  await w.findAll("button")[5].trigger("click");
  expect(w.emitted("update:modelValue")).toEqual([["done"]]);
});

test("the pills show only on the phone", () => {
  const w = mount(StatusPills, { props: { counts, modelValue: "todo" } });
  expect(w.classes()).toContain("hidden");
  expect(w.classes()).toContain("max-[640px]:flex");
});
