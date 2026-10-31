import { mount } from "@vue/test-utils";
import { detail, job, step } from "@/test-utils/workflow-fixtures";
import JobDetail from "./JobDetail.vue";

const show = (d = detail(), nodeLabel: string | null = "Layer 02") => mount(JobDetail, { props: { detail: d, nodeLabel: nodeLabel ?? undefined } });

test("the head names the job, its status and role, and the run it came from", () => {
  const w = show(detail({ job: job({ label: "Build", status: "failed", studio_run: "r-1" }) }));
  expect(w.get("h2").text()).toBe("Layer 02 / Build");
  expect(w.text()).toContain("failed");
  expect(w.text()).toContain("coder");
  expect(w.text()).toContain("run r-1");
});

test("without a node label the head is the job's own", () => {
  expect(show(detail(), null).get("h2").text()).toBe("Build");
});

test("its numbers: attempts, duration and, for a model job, the speed", () => {
  const w = show(detail({ job: job({ tokens: { steps: 3, prompt: 1000, completion: 200, gen_s: 1, prefill_s: 1, tool_s: 0, tps: 38.2, effective_tps: 21, prefill_tps: 1240, role: "tester", model: "m" } }) }));
  expect(w.text()).toContain("38.2 tok/s");
  expect(w.text()).toContain("Write speed");
  expect(w.text()).toContain("Read speed");
  expect(w.text()).toContain("1240 tok/s");
  expect(w.text()).toContain("2 min 5 s");
});

test("why it retried lists each retry's kind and detail, only when there were any", () => {
  expect(show().text()).not.toContain("Why it retried");
  const w = show(detail({ job: job({ retries: [{ kind: "no_new_test", detail: "no new test" }] }) }));
  expect(w.text()).toContain("Why it retried");
  expect(w.text()).toContain("no_new_test");
  expect(w.text()).toContain("no new test");
});

test("the input is the prompt's messages by role; with none recorded it says why", () => {
  const w = show(detail({ input: { messages: [{ role: "system", content: "You are the tester." }, { role: "user", content: "Goal: write the failing tests" }] }, input_note: null }));
  expect(w.text()).toContain("system");
  expect(w.text()).toContain("Goal: write the failing tests");
  expect(show().text()).toContain("opening prompt not recorded");
});

test("the output labels what the job produced", () => {
  const w = show(detail({ output: { final_text: "done", layer: { title: "x" }, built: { sha: "abc" } } }));
  expect(w.text()).toContain("final reply");
  expect(w.text()).toContain("layer brief");
  expect(w.text()).toContain("\"sha\": \"abc\"");
});

test("a running job with nothing yet says the output comes at the end; an ended one says nothing was recorded", () => {
  expect(show(detail({ job: job({ status: "running" }) })).text()).toContain("Still running. The output appears when the job ends.");
  expect(show().text()).toContain("Nothing recorded.");
});

test("the job is not itself a live region", () => {
  expect(show().get("section").attributes("aria-live")).toBeUndefined();
});

test("the steps are listed with their count", () => {
  const w = show(detail({ steps: [step(), step({ n: 2 })] }));
  expect(w.findAll("details[data-step]")).toHaveLength(2);
  expect(w.text()).toContain("Steps (2)");
  expect(w.findAll("details")).toHaveLength(2);
  expect(show().text()).not.toContain("Steps (");
});
