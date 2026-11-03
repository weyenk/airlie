import { mount } from "@vue/test-utils";
import { exitRow } from "@/test-utils/workflow-fixtures";
import ExitRow from "./ExitRow.vue";

test("a blocked story's exit shows where, how often, and its jobs as boxes", async () => {
  const w = mount(ExitRow, { props: { exit: exitRow() } });
  const row = w.get("[data-exit=blocked]");
  expect(row.text()).toContain("Blocked");
  expect(row.text()).toContain("L02 build 2 of 2");
  expect(row.text()).toContain("blocked 3 times");
  expect(row.findAll("[data-job]").map((b) => b.attributes("data-job"))).toEqual(["diagnose", "fix"]);
  await row.get("[data-job=fix]").trigger("click");
  expect(w.emitted("pick")).toEqual([["exit", "fix"]]);
});

test("one block carries no count tag; a waiting exit names its key", () => {
  const w = mount(ExitRow, { props: { exit: exitRow({ kind: "waiting", times: 1, on: "AIR-5", jobs: [] }) } });
  expect(w.get("[data-exit=waiting]").text()).toContain("AIR-5");
  expect(w.text()).not.toContain("times");
});
