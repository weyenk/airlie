import { mount } from "@vue/test-utils";
import ProgressBar from "./ProgressBar.vue";

test("a value fills the bar and null slides", () => {
  const w = mount(ProgressBar, { props: { value: 40 } });
  expect(w.find("i").attributes("style")).toMatch(/width: 40%/);
  expect(w.find("[data-indeterminate]").exists()).toBe(false);
  const n = mount(ProgressBar, { props: { value: null } });
  expect(n.find("[data-indeterminate]").exists()).toBe(true);
});
test("a value outside 0 to 100 is clamped", () => {
  expect(mount(ProgressBar, { props: { value: 140 } }).find("i").attributes("style")).toMatch(/width: 100%/);
});
