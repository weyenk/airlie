import { mount } from "@vue/test-utils";
import { step } from "@/test-utils/workflow-fixtures";
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

async function open(w: ReturnType<typeof mount>, i = 0) {
  const d = w.findAll("details")[i];
  (d.element as HTMLDetailsElement).open = true;
  await d.trigger("toggle");
}

test("a step opens its thinking, its text and what the tool returned", async () => {
  const w = mount(StepList, { props: { steps: [step({ reasoning: "start from the existing tests", assistant_text: "reading", observation: "1: x" })] } });
  await open(w);
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

test("a closed step builds no body; opening it builds one", async () => {
  const w = mount(StepList, { props: { steps: [step({ n: 1, reasoning: "secret" }), step({ n: 2 })] } });
  expect(w.findAll("[data-step-body]")).toHaveLength(0);
  expect(w.text()).not.toContain("secret");
  await open(w, 0);
  expect(w.findAll("[data-step-body]")).toHaveLength(1);
});

test("an open step stays open when new step objects arrive (a live refresh)", async () => {
  const w = mount(StepList, { props: { steps: [step({ n: 1, reasoning: "keep me" })] } });
  await open(w);
  await w.setProps({ steps: [step({ n: 1, reasoning: "keep me" }), step({ n: 2 })] });
  expect(w.text()).toContain("keep me");
  expect((w.findAll("details")[0].element as HTMLDetailsElement).open).toBe(true);
  expect((w.findAll("details")[1].element as HTMLDetailsElement).open).toBe(false);
});
