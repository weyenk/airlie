import { mount } from "@vue/test-utils";
import StatusChip from "./StatusChip.vue";
import { STATUS_TABLE } from "./statusTable";

test("status chips use one table for every kind", () => {
  const review = mount(StatusChip, { props: { kind: "stage", value: "review" } });
  expect(review.text()).toBe("in review");
  expect(review.classes().join(" ")).toMatch(/bg-warn/);
  const failed = mount(StatusChip, { props: { kind: "job", value: "failed" } });
  expect(failed.text()).toBe("failed");
  expect(failed.classes().join(" ")).toMatch(/bg-danger/);
  expect(mount(StatusChip, { props: { kind: "board", value: "todo" } }).text()).toBe("To do");
  expect(mount(StatusChip, { props: { kind: "board", value: "review" } }).text()).toBe("Review");
  expect(mount(StatusChip, { props: { kind: "run", value: "studio" } }).text()).toBe("studio run");
  for (const kind of ["board", "stage", "job", "run"] as const) expect(Object.keys(STATUS_TABLE[kind]).length).toBeGreaterThan(2);
});

test("an unknown value shows as a neutral chip with its own words", () => {
  const w = mount(StatusChip, { props: { kind: "job", value: "odd" } });
  expect(w.text()).toBe("odd");
  expect(w.classes().join(" ")).toMatch(/bg-chip/);
});
