import { flushPromises, mount } from "@vue/test-utils";
import PlanCard from "./PlanCard.vue";

const PLAN = { type: "plan", assumptions: ["a1", "a2"], subtasks: [{ id: "fix", role: "coder", brief: "do it", depends_on: [] }, { id: "doc", role: null, brief: "write", depends_on: ["fix"] }] };

afterEach(() => {
  document.body.innerHTML = "";
});

test("assumptions, the subtask table, and the two buttons", () => {
  const w = mount(PlanCard, { props: { plan: PLAN } });
  expect(w.findAll("[data-assumptions] li").map((l) => l.text())).toEqual(["a1", "a2"]);
  expect(w.findAll("th").map((h) => h.text())).toEqual(["Subtask", "Role", "Brief", "After"]);
  expect(w.findAll("[data-subtask]").map((r) => r.findAll("td").map((c) => c.text()))).toEqual([
    ["fix", "coder", "do it", "—"],
    ["doc", "", "write", "fix"],
  ]);
  expect(w.get("[data-approve]").text()).toBe("Approve plan");
  expect(w.get("[data-change]").text()).toBe("Change it");
});

test("a plan with odd fields still draws (assumptions as a string, a subtask that is not an object)", () => {
  const w = mount(PlanCard, { props: { plan: { assumptions: "just one", subtasks: ["oops", null] } } });
  expect(w.findAll("[data-assumptions] li").map((l) => l.text())).toEqual(["just one"]);
  expect(w.findAll("[data-subtask]")).toHaveLength(2);
});

test("Approve emits the plan; while busy it cannot be pressed", async () => {
  const w = mount(PlanCard, { props: { plan: PLAN } });
  await w.get("[data-approve]").trigger("click");
  expect(w.emitted("approve")).toEqual([[PLAN]]);
  await w.setProps({ busy: true });
  expect(w.get("[data-approve]").attributes("disabled")).toBeDefined();
});

test("an edit is validated, then replaces the plan Approve emits", async () => {
  const w = mount(PlanCard, { props: { plan: PLAN }, attachTo: document.body });
  await w.get("[data-change]").trigger("click");
  await flushPromises();
  const area = document.body.querySelector<HTMLTextAreaElement>("[data-plan-json]")!;
  area.value = '{"subtasks": [';
  area.dispatchEvent(new Event("input"));
  await flushPromises();
  expect(document.body.querySelector("[data-plan-problem]")!.textContent).toContain("That is not valid JSON");
  const edit = { subtasks: [{ id: "only", brief: "x" }] };
  area.value = JSON.stringify(edit);
  area.dispatchEvent(new Event("input"));
  await flushPromises();
  document.body.querySelector<HTMLButtonElement>("[data-plan-save]")!.click();
  await flushPromises();
  await w.get("[data-approve]").trigger("click");
  expect(w.emitted("approve")).toEqual([[edit]]);
  expect(w.text()).toContain("edited");
});
