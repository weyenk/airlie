import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { ApiError } from "@/api";
import type { Flow, FlowNode } from "@/api/workflows";
import { answerConfirm, confirmState } from "@/composables/useConfirm";
import { detail, flow, job, stage } from "./workflow/fixtures";
import RunWorkflowScreen from "./RunWorkflowScreen.vue";

const api = vi.hoisted(() => ({ runFlow: vi.fn(), runJob: vi.fn(), cancelJob: vi.fn(), startRun: vi.fn() }));
vi.mock("@/api/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflows")>()), ...api }));

const ID = "r-1024-1530";
const node = (id: string, label: string, status: FlowNode["status"], jobs: Partial<ReturnType<typeof job>>[]): FlowNode => ({
  id,
  label,
  kind: "stage",
  status,
  jobs: jobs.map((j) => job({ role: null, studio_run: ID, ...j })),
});
const run = (nodes: FlowNode[], over: Partial<Flow> = {}): Flow =>
  flow(nodes, { key: ID, kind: "run", title: "Speed up repo map rebuilds", repo: "weyenk/airlie", topology: "teams", stage: "done", ok: true, progress: 1, job: null, plan: null, ...over });

const TEAMS = run([
  node("teams", "Teams", "passed", [
    { id: "team-1", label: "Team 1", status: "failed" },
    { id: "team-2", label: "Team 2", status: "passed", won: true },
    { id: "team-3", label: "Team 3", status: "passed" },
  ]),
  node("judge", "Judge", "passed", [{ id: "aggregate", label: "Judge" }]),
]);
const PLAN = {
  type: "plan",
  assumptions: ["The flake is the upload finishing after the assertion."],
  subtasks: [
    { id: "fix", role: "coder", brief: "Wait for the upload's response.", depends_on: [], inputs: [] },
    { id: "retry-note", role: "docs", brief: "Note the wait in the docstring.", depends_on: ["fix"], inputs: [] },
  ],
};
const PLANNED = run([node("plan", "Plan", "passed", [{ id: "chief", label: "Chief", status: "passed" }])], { topology: "studio", job: "done", plan: PLAN, title: "Fix the flaky upload test" });

async function open(url = `/workflows/runs/${ID}`, f: Flow = TEAMS) {
  api.runFlow.mockResolvedValue(f);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/workflows", component: { template: "<i/>" } },
      { path: "/workflows/runs/:id/:node?/:job?", name: "run", component: RunWorkflowScreen },
    ],
  });
  await router.push(url);
  const w = mount(RunWorkflowScreen, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return { w, router };
}

beforeEach(() => {
  setActivePinia(createPinia());
  api.runFlow.mockReset();
  api.runJob.mockReset().mockImplementation(async (_i: string, n: string, jobId: string) => detail({ node: n, job: job({ id: jobId, label: jobId }) }));
  api.cancelJob.mockReset().mockResolvedValue({ job_id: ID, cancelled: true });
  api.startRun.mockReset().mockResolvedValue({ job_id: "r-new", kind: "run" });
});
afterEach(() => {
  document.body.innerHTML = "";
  answerConfirm(false);
});

test("a teams run draws the Teams stage with its three jobs, the winner starred, then the Judge", async () => {
  const { w } = await open();
  const stages = w.findAll("[data-stage]");
  expect(stages.map((s) => s.attributes("data-node"))).toEqual(["teams", "judge"]);
  const teams = stages[0].findAll("button[data-job]");
  expect(teams.map((b) => b.attributes("data-job"))).toEqual(["team-1", "team-2", "team-3"]);
  expect(teams.map((b) => b.find("[data-won]").exists())).toEqual([false, true, false]);
  expect(teams[1].text()).toContain("★ won");
  expect(w.findAll("[data-won]")).toHaveLength(1);
});

test("the head: kind tag, state, run id and repository, the goal, back to Workflows", async () => {
  const { w } = await open();
  expect(w.get("a[href='/workflows']").text()).toContain("Workflows");
  expect(w.text()).toContain("teams run");
  expect(w.text()).toContain("done");
  expect(w.text()).toContain(`${ID} · weyenk/airlie`);
  expect(w.get("h1").text()).toBe("Speed up repo map rebuilds");
});

test("a goal is text, never markup", async () => {
  const { w } = await open(`/workflows/runs/${ID}`, run(TEAMS.nodes, { title: "<img src=x onerror=alert(1)> <b>bold</b>" }));
  expect(w.get("h1").text()).toBe("<img src=x onerror=alert(1)> <b>bold</b>");
  expect(w.find("h1 img").exists()).toBe(false);
  expect(w.find("h1 b").exists()).toBe(false);
});

test("a run that is not there says so", async () => {
  api.runFlow.mockRejectedValue(new ApiError(404, "no run"));
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/workflows", component: { template: "<i/>" } }, { path: "/workflows/runs/:id/:node?/:job?", component: RunWorkflowScreen }] });
  await router.push("/workflows/runs/nope");
  const w = mount(RunWorkflowScreen, { global: { plugins: [router] } });
  await flushPromises();
  expect(w.get("[role=alert]").text()).toBe("No run nope.");
});

test("single and fork runs draw their stages (the fork's winner starred)", async () => {
  const single = run([node("agent", "Agent", "running", [{ id: "team-1", label: "Agent", status: "running" }])], { topology: "single", stage: "running", ok: null, progress: null });
  const { w } = await open(`/workflows/runs/${ID}`, single);
  expect(w.findAll("[data-stage]").map((s) => s.attributes("data-node"))).toEqual(["agent"]);
  expect(w.text()).toContain("single run");

  const fork = run(
    [
      node("agent", "Agent", "failed", [{ id: "b0", label: "Chief", status: "failed" }]),
      node("forks", "Forks", "passed", [
        { id: "b1", label: "Fork 1", status: "passed", won: true },
        { id: "b2", label: "Fork 2", status: "failed" },
      ]),
    ],
    { topology: "fork" },
  );
  const again = await open(`/workflows/runs/${ID}`, fork);
  expect(again.w.findAll("[data-stage]").map((s) => s.attributes("data-node"))).toEqual(["agent", "forks"]);
  expect(again.w.get("[data-stage][data-node=forks] [data-job=b1]").text()).toContain("★ won");
});

test("Stop shows only while the server's job for the run is running, and asks first", async () => {
  const running = run(TEAMS.nodes, { job: "running", stage: "running", ok: null, progress: 0.4 });
  const { w } = await open(`/workflows/runs/${ID}`, running);
  await w.get("[data-stop]").trigger("click");
  expect(confirmState.open).toBe(true);
  expect(api.cancelJob).not.toHaveBeenCalled();
  answerConfirm(true);
  await flushPromises();
  expect(api.cancelJob).toHaveBeenCalledWith(ID);
});

test("a declined Stop cancels nothing", async () => {
  const { w } = await open(`/workflows/runs/${ID}`, run(TEAMS.nodes, { job: "running", stage: "running", ok: null, progress: 0.4 }));
  await w.get("[data-stop]").trigger("click");
  answerConfirm(false);
  await flushPromises();
  expect(api.cancelJob).not.toHaveBeenCalled();
});

test.each([[null], ["done"], ["failed"], ["cancelled"]])("no Stop when the server's job is %s", async (state) => {
  const { w } = await open(`/workflows/runs/${ID}`, run(TEAMS.nodes, { job: state }));
  expect(w.find("[data-stop]").exists()).toBe(false);
});

test("a failed Stop says why", async () => {
  api.cancelJob.mockRejectedValue(new Error("boom"));
  const { w } = await open(`/workflows/runs/${ID}`, run(TEAMS.nodes, { job: "running", stage: "running", progress: 0.4 }));
  await w.get("[data-stop]").trigger("click");
  answerConfirm(true);
  await flushPromises();
  expect(w.get("[role=alert]").text()).toBe("boom");
});

test("the progress figure shows while the run goes and is gone once it is done, even below 1", async () => {
  const going = await open(`/workflows/runs/${ID}`, run(TEAMS.nodes, { stage: "running", ok: null, progress: 0.4 }));
  expect(going.w.get("[data-progress]").text()).toBe("40%");
  expect(going.w.find("[role=progressbar]").exists()).toBe(true);
  document.body.innerHTML = "";
  const done = await open(`/workflows/runs/${ID}`, run(TEAMS.nodes, { stage: "done", progress: 0.8 }));
  expect(done.w.find("[data-progress]").exists()).toBe(false);
  expect(done.w.find("[role=progressbar]").exists()).toBe(false);
  expect(done.w.text()).not.toContain("80%");
});

test("the failed job is open by default and stays on screen when the run moves on", async () => {
  const f = run([node("agent", "Agent", "failed", [{ id: "b0", label: "Chief", status: "failed" }]), node("forks", "Forks", "running", [{ id: "b1", label: "Fork 1", status: "running" }])], { topology: "fork", stage: "running", ok: null, progress: 0.5 });
  const { w } = await open(`/workflows/runs/${ID}`, f);
  expect(api.runJob).toHaveBeenLastCalledWith(ID, "agent", "b0");
  expect(w.get("[data-job=b0]").attributes("aria-pressed")).toBe("true");
});

test("a job in the address wins, and clicking a box opens it under the run", async () => {
  const { w, router } = await open(`/workflows/runs/${ID}/teams/team-3`);
  expect(api.runJob).toHaveBeenLastCalledWith(ID, "teams", "team-3");
  await w.get("[data-job=team-2]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.path).toBe(`/workflows/runs/${ID}/teams/team-2`);
  expect(api.runJob).toHaveBeenLastCalledWith(ID, "teams", "team-2");
});

test("a link to a job that is not there opens the default one", async () => {
  await open(`/workflows/runs/${ID}/teams/nope`);
  expect(api.runJob).toHaveBeenLastCalledWith(ID, "teams", "team-1"); // the failed one
});

test("the plan card shows the assumptions and the subtasks, as text", async () => {
  const evil = { ...PLAN, assumptions: ["<b>bold</b>"], subtasks: [{ id: "<i>x</i>", role: "coder", brief: "<script>alert(1)</script>", depends_on: [] }] };
  const { w } = await open(`/workflows/runs/${ID}`, { ...PLANNED, plan: evil });
  const card = w.get("section[aria-label=Plan]");
  expect(card.get("[data-assumptions]").text()).toBe("<b>bold</b>");
  expect(card.get("[data-subtask]").text()).toContain("<script>alert(1)</script>");
  expect(card.find("b, i, script").exists()).toBe(false);
});

test("a finished plan job shows the plan card; a run without a plan shows none", async () => {
  const withPlan = await open(`/workflows/runs/${ID}`, PLANNED);
  const rows = withPlan.w.findAll("[data-subtask]");
  expect(rows).toHaveLength(2);
  expect(rows[1].text()).toContain("retry-note");
  expect(rows[1].text()).toContain("docs");
  expect(rows[1].text()).toContain("fix"); // After
  expect(rows[0].text()).toContain("—");
  expect(withPlan.w.text()).toContain("waiting for your approval");
  document.body.innerHTML = "";
  const none = await open(`/workflows/runs/${ID}`, TEAMS);
  expect(none.w.find("section[aria-label=Plan]").exists()).toBe(false);
});

test("Approve plan posts the plan with the run's goal and repository, then opens the new run", async () => {
  const { w, router } = await open(`/workflows/runs/${ID}`, PLANNED);
  await w.get("[data-approve]").trigger("click");
  await flushPromises();
  expect(api.startRun).toHaveBeenCalledWith({ goal: "Fix the flaky upload test", repo: "weyenk/airlie", plan: PLAN });
  expect(router.currentRoute.value.path).toBe("/workflows/runs/r-new");
});

test("a plan the server turns down is shown and the view stays", async () => {
  api.startRun.mockRejectedValue(new ApiError(400, "plan rejected: role 'chief' cannot be a subtask"));
  const { w, router } = await open(`/workflows/runs/${ID}`, PLANNED);
  await w.get("[data-approve]").trigger("click");
  await flushPromises();
  expect(w.get("[role=alert]").text()).toBe("plan rejected: role 'chief' cannot be a subtask");
  expect(router.currentRoute.value.path).toBe(`/workflows/runs/${ID}`);
});

async function typeJson(text: string) {
  const area = document.body.querySelector<HTMLTextAreaElement>("[data-plan-json]")!;
  area.value = text;
  area.dispatchEvent(new Event("input", { bubbles: true }));
  await flushPromises();
}

test("Change it opens the plan as JSON; the edit is what Approve sends", async () => {
  const { w } = await open(`/workflows/runs/${ID}`, PLANNED);
  await w.get("[data-change]").trigger("click");
  await flushPromises();
  const area = document.body.querySelector<HTMLTextAreaElement>("[data-plan-json]")!;
  expect(JSON.parse(area.value)).toEqual(PLAN);
  const edited = { ...PLAN, subtasks: PLAN.subtasks.slice(0, 1) };
  await typeJson(JSON.stringify(edited));
  document.body.querySelector<HTMLButtonElement>("[data-plan-save]")!.click();
  await flushPromises();
  expect(w.findAll("[data-subtask]")).toHaveLength(1); // the card shows the edit
  await w.get("[data-approve]").trigger("click");
  await flushPromises();
  expect(api.startRun).toHaveBeenCalledWith({ goal: "Fix the flaky upload test", repo: "weyenk/airlie", plan: edited });
});

test.each([
  ["{ not json", /^That is not valid JSON: /],
  ["[1, 2]", /must be a JSON object/],
  ['{"subtasks": []}', /needs a list of subtasks/],
])("JSON that is not a plan (%s) is explained in words and cannot be used", async (text, words) => {
  const { w } = await open(`/workflows/runs/${ID}`, PLANNED);
  await w.get("[data-change]").trigger("click");
  await flushPromises();
  await typeJson(text);
  expect(document.body.querySelector("[data-plan-problem]")!.textContent).toMatch(words);
  expect(document.body.querySelector<HTMLButtonElement>("[data-plan-save]")!.disabled).toBe(true);
  await typeJson(JSON.stringify(PLAN));
  expect(document.body.querySelector("[data-plan-problem]")).toBeNull();
  expect(document.body.querySelector<HTMLButtonElement>("[data-plan-save]")!.disabled).toBe(false);
});

test("cancelling the dialog keeps the plan as it was", async () => {
  const { w } = await open(`/workflows/runs/${ID}`, PLANNED);
  await w.get("[data-change]").trigger("click");
  await flushPromises();
  await typeJson("{}");
  document.body.querySelector<HTMLButtonElement>("[data-plan-cancel]")!.click();
  await flushPromises();
  await w.get("[data-approve]").trigger("click");
  await flushPromises();
  expect(api.startRun).toHaveBeenCalledWith(expect.objectContaining({ plan: PLAN }));
});

test("a run that does not name its repository cannot be approved, and says why", async () => {
  const { w } = await open(`/workflows/runs/${ID}`, { ...PLANNED, repo: null });
  await w.get("[data-approve]").trigger("click");
  await flushPromises();
  expect(api.startRun).not.toHaveBeenCalled();
  expect(w.get("[role=alert]").text()).toContain("repository");
});

test("an empty stage never ran and is left out", async () => {
  const f = run([stage("plan", "Plan"), { id: "specialists", label: "Specialists", kind: "stage", status: "pending", jobs: [] }], { topology: "studio" });
  const { w } = await open(`/workflows/runs/${ID}`, f);
  expect(w.findAll("[data-stage]").map((s) => s.attributes("data-node"))).toEqual(["plan"]);
});
