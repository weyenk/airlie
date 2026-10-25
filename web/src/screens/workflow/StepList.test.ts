import { mount } from "@vue/test-utils";
import { step } from "./fixtures";
import StepList from "./StepList.vue";

test("a step shows its number, tool and arguments; a run's retry round prefixes the number", () => {
  const w = mount(StepList, { props: { steps: [step({ n: 3, tool: "write_file", arguments: "{\"path\": \"a.py\"}" }), step({ attempt: 2, n: 1 })] } });
  const rows = w.findAll("details");
  expect(rows[0].get("summary").text()).toContain("3");
  expect(rows[0].get("summary").text()).toContain("write_file");
  expect(rows[0].get("summary").text()).toContain("{\"path\": \"a.py\"}");
  expect(rows[1].get("summary").text()).toContain("r2·1");
});

test("arguments that are an object are written out, and a step with no tool says so", () => {
  const w = mount(StepList, { props: { steps: [step({ tool: null, arguments: { path: "a.py" } })] } });
  expect(w.text()).toContain("(no tool)");
  expect(w.text()).toContain("{\"path\":\"a.py\"}");
});

test("a step opens its thinking, its text and what the tool returned", () => {
  const w = mount(StepList, { props: { steps: [step({ reasoning: "start from the existing tests", assistant_text: "reading", observation: "1: x" })] } });
  const body = w.get("details");
  expect(body.text()).toContain("thinking");
  expect(body.text()).toContain("start from the existing tests");
  expect(body.text()).toContain("reading");
  expect(body.text()).toContain("1: x");
});

test("a failed step says so; a step that worked says ok", () => {
  const w = mount(StepList, { props: { steps: [step({ ok: false }), step({ ok: true }), step({ ok: null })] } });
  const [bad, good, unknown] = w.findAll("summary").map((s) => s.text());
  expect(bad).toContain("failed");
  expect(good).toContain("ok");
  expect(unknown).not.toMatch(/failed|ok$/);
});
