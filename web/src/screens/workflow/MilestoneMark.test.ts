import { mount } from "@vue/test-utils";
import MilestoneMark from "./MilestoneMark.vue";

test("a milestone is a diamond with its name and time", () => {
  const w = mount(MilestoneMark, { props: { label: "PLANNED", status: "passed", at: "15:15" } });
  expect(w.attributes("data-shape")).toBe("milestone");
  expect(w.attributes("data-status")).toBe("passed");
  expect(w.text()).toContain("PLANNED");
  expect(w.text()).toContain("15:15");
  expect(w.find("[data-diamond]").classes()).toContain("rotate-45");
});

test("a milestone not yet reached is grey", () => {
  const w = mount(MilestoneMark, { props: { label: "PLANNED", status: "pending" } });
  expect(w.find("[data-diamond]").classes()).toContain("bg-track");
});
