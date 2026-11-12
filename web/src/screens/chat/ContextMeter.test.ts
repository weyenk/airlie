import { mount } from "@vue/test-utils";
import ContextMeter from "./ContextMeter.vue";

const show = (used: number | null, limit: number | null) => mount(ContextMeter, { props: { used, limit } });

test("it reads used of limit as a progressbar", () => {
  const w = show(118_000, 262_144);
  expect(w.text()).toBe("118k / 262k");
  const bar = w.get('[role="progressbar"]');
  expect(bar.attributes("aria-valuenow")).toBe("118000");
  expect(bar.attributes("aria-valuemax")).toBe("262144");
  expect(bar.attributes("aria-valuetext")).toBe("118k of 262k tokens");
});

test("it warns at ninety percent", () => {
  const near = show(236_000, 262_144); // 90.02 %
  expect(near.get('[role="progressbar"]').attributes("aria-valuetext")).toBe("236k of 262k tokens, nearly full");
  expect(near.get("[data-meter]").attributes("data-warn")).toBe("true");
  const below = show(235_000, 262_144); // 89.6 %
  expect(below.get('[role="progressbar"]').attributes("aria-valuetext")).toBe("235k of 262k tokens");
  expect(below.get("[data-meter]").attributes("data-warn")).toBeUndefined();
});

test("before any finish it shows nothing", () => {
  expect(show(null, null).find('[role="progressbar"]').exists()).toBe(false);
  expect(show(null, null).text()).toBe("");
});
