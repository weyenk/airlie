import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { ApiError } from "@/api";
import type { Flow, JobRow } from "@/api/workflows";
import { answerConfirm, confirmState } from "@/composables/useConfirm";
import { detail, flow, job, layer, stage } from "./workflow/fixtures";
import WorkflowScreen from "./WorkflowScreen.vue";

const api = vi.hoisted(() => ({ storyFlow: vi.fn(), storyJob: vi.fn(), listJobs: vi.fn(), listRunning: vi.fn(), cancelJob: vi.fn() }));
vi.mock("@/api/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflows")>()), ...api }));

const STAGES = [stage("intake", "Intake"), stage("refine", "Refine"), stage("layers", "Plan layers")];
const FAILED = flow([...STAGES, layer("01", "Comment id store", "passed", ["passed", "passed", "passed"]), layer("02", "Comment edit", "failed", ["passed", "failed", "pending"]), stage("publish", "Publish", "pending")], {
  blocked: "layer 02: its tests still do not test its change",
  stage: "layers",
});
const RUNNING = flow([...STAGES, layer("01", "Comment id store", "passed", ["passed", "passed", "passed"]), layer("02", "Comment edit", "running", ["passed", "passed", "running"]), stage("publish", "Publish", "pending")]);
const DONE = flow([...STAGES, layer("01", "Comment id store", "passed", ["passed", "passed", "passed"]), stage("publish", "Publish")], { stage: "publish" });

async function open(url = "/workflows/AIR-17", f: Flow = FAILED) {
  api.storyFlow.mockResolvedValue(f);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/workflows", component: { template: "<i/>" } },
      { path: "/workflows/:key/:node?/:job?", name: "story", component: WorkflowScreen },
    ],
  });
  await router.push(url);
  const w = mount(WorkflowScreen, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return { w, router };
}
const lastDetailCall = () => api.storyJob.mock.calls.at(-1);
const jobRow = (over: Partial<JobRow> = {}): JobRow => ({ id: "story-AIR-17", kind: "story", state: "running", started: 1, ...over });

beforeEach(() => {
  setActivePinia(createPinia());
  api.storyFlow.mockReset();
  api.storyJob.mockReset().mockImplementation(async (_k: string, node: string, jobId: string) => detail({ node, job: job({ id: jobId, label: jobId }) }));
  api.listJobs.mockReset().mockResolvedValue([]);
  api.listRunning.mockReset().mockResolvedValue([]);
  api.cancelJob.mockReset().mockResolvedValue({ job_id: "story-AIR-17", cancelled: true });
});
afterEach(() => {
  document.body.innerHTML = "";
  answerConfirm(false);
});

test("the head: back to Workflows, the key, the stage in progress, the Board card link, the title and why it is blocked", async () => {
  const { w } = await open("/workflows/AIR-17", flow(STAGES, { blocked: null, stage: "layers" }));
  expect(w.get("a[href='/workflows']").text()).toContain("Workflows");
  expect(w.get("h1").text()).toBe("Board card modal");
  expect(w.text()).toContain("AIR-17");
  expect(w.text()).toContain("building"); // layers is the last stage finished: building is next
  expect(w.get("a[href='/board/AIR-17']").text()).toBe("Board card");
  expect(w.find("[role=alert]").exists()).toBe(false);
});

test("a blocked story says blocked and shows its reason", async () => {
  const { w } = await open();
  expect(w.text()).toContain("blocked");
  expect(w.get("[role=alert]").text()).toBe("layer 02: its tests still do not test its change");
});

test("with no job in the address it opens on the failed job", async () => {
  const { w } = await open();
  expect(lastDetailCall()).toEqual(["AIR-17", "layer-02", "checks"]);
  expect(w.get("[data-lane][data-node=layer-02] [data-job=checks]").attributes("aria-pressed")).toBe("true");
});

test("else the running job, else the last that ran", async () => {
  await open("/workflows/AIR-17", RUNNING);
  expect(lastDetailCall()).toEqual(["AIR-17", "layer-02", "build"]);
  await open("/workflows/AIR-17", DONE);
  expect(lastDetailCall()).toEqual(["AIR-17", "publish", "publish"]);
});

test("a job named in the address is the one that opens; one that does not exist falls back", async () => {
  await open("/workflows/AIR-17/layer-01/tests");
  expect(lastDetailCall()).toEqual(["AIR-17", "layer-01", "tests"]);
  await open("/workflows/AIR-17/layer-09/tests");
  expect(lastDetailCall()).toEqual(["AIR-17", "layer-02", "checks"]);
});

test("picking a box puts the job in the address and shows its detail", async () => {
  const { w, router } = await open();
  await w.get("[data-lane][data-node=layer-01] [data-job=tests]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/workflows/AIR-17/layer-01/tests");
  expect(lastDetailCall()).toEqual(["AIR-17", "layer-01", "tests"]);
  expect(w.get("section[aria-live=polite] h2").text()).toBe("Layer 01 / tests");
});

test("a story with nothing run yet says so instead of a job", async () => {
  const { w } = await open("/workflows/AIR-17", flow([stage("intake", "Intake", "pending"), stage("publish", "Publish", "pending")], { stage: null }));
  expect(w.text()).toContain("Nothing has run yet.");
  expect(api.storyJob).not.toHaveBeenCalled();
});

test("Stop shows only while this server runs the story's job, and stops it after a confirm", async () => {
  api.listJobs.mockResolvedValue([jobRow()]);
  const { w } = await open("/workflows/AIR-17", RUNNING);
  const stop = w.get("button[data-stop]");
  await stop.trigger("click");
  expect(confirmState.open).toBe(true);
  answerConfirm(true);
  await flushPromises();
  expect(api.cancelJob).toHaveBeenCalledWith("story-AIR-17");
});

test("declining the confirm stops nothing", async () => {
  api.listJobs.mockResolvedValue([jobRow()]);
  const { w } = await open("/workflows/AIR-17", RUNNING);
  await w.get("button[data-stop]").trigger("click");
  answerConfirm(false);
  await flushPromises();
  expect(api.cancelJob).not.toHaveBeenCalled();
});

test("no Stop when no job of this server runs the story, or the job is another's or ended", async () => {
  expect((await open("/workflows/AIR-17", RUNNING)).w.find("[data-stop]").exists()).toBe(false);
  api.listJobs.mockResolvedValue([jobRow({ id: "story-AIR-9" }), jobRow({ state: "done" })]);
  expect((await open("/workflows/AIR-17", RUNNING)).w.find("[data-stop]").exists()).toBe(false);
});

test("a story that does not exist says so", async () => {
  api.storyFlow.mockReset().mockRejectedValue(new ApiError(404, "no story 'AIR-99'"));
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/workflows", component: { template: "<i/>" } }, { path: "/workflows/:key/:node?/:job?", component: WorkflowScreen }] });
  await router.push("/workflows/AIR-99");
  const w = mount(WorkflowScreen, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  expect(w.text()).toContain("No story AIR-99.");
});

test("a failed detail load shows its error", async () => {
  api.storyJob.mockReset().mockRejectedValue(new Error("boom"));
  const { w } = await open();
  expect(w.text()).toContain("boom");
});

test("the progress figure uses the running list's number for this story", async () => {
  api.listRunning.mockResolvedValue([{ kind: "story", key: "AIR-17", title: "x", topology: null, progress: 0.62, last_activity: 1 }]);
  const { w } = await open("/workflows/AIR-17", RUNNING);
  expect(w.get("[data-progress]").text()).toBe("62% · 1 of 2 layers built");
});
