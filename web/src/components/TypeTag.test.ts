import { mount } from "@vue/test-utils";
import TypeTag from "./TypeTag.vue";

test("a type shows its colour square and name", () => {
  const w = mount(TypeTag, { props: { type: "spike" } });
  expect(w.text()).toBe("spike");
  expect(w.find("[data-square]").attributes("style")).toMatch(/#fb923c|rgb\(251, 146, 60\)/);
});
test("an unknown type is grey, not broken", () => {
  expect(mount(TypeTag, { props: { type: "mystery" } }).text()).toBe("mystery");
});
