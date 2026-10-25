import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { ApiError } from "@/api";
import type { Flow, JobRow } from "@/api/workflows";
import { useFeedStore } from "@/stores/feed";
import { answerConfirm, confirmState } from "@/composables/useConfirm";
import { detail, flow, job, layer, stage, step } from "./workflow/fixtures";
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
  expect(w.get("section[aria-label=Job] h2").text()).toBe("Layer 01 / tests");
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

test("a live refresh keeps the job on show, though the default would now be another", async () => {
  const { w } = await open(); // the failed job opens: layer 02, checks
  expect(lastDetailCall()).toEqual(["AIR-17", "layer-02", "checks"]);
  api.storyFlow.mockResolvedValue(RUNNING); // now layer 02's build runs, which is what a fresh page would open on
  api.storyJob.mockClear();
  useFeedStore().generation += 1;
  await flushPromises();
  expect(api.storyFlow).toHaveBeenCalledTimes(2);
  expect(w.get("[data-lane][data-node=layer-02] [data-job=checks]").attributes("aria-pressed")).toBe("true");
  expect(w.get("[data-lane][data-node=layer-02] [data-job=build]").attributes("aria-pressed")).toBe("false");
  expect(api.storyJob.mock.calls.every((c) => c[1] === "layer-02" && c[2] === "checks")).toBe(true);
});

test("a step a reader opened stays open when the detail refreshes", async () => {
  api.storyJob.mockImplementation(async () => detail({ node: "layer-02", job: job({ id: "checks", label: "checks" }), steps: [step({ reasoning: "think first" })] }));
  const { w } = await open();
  const d = w.get("details[data-step]");
  (d.element as HTMLDetailsElement).open = true;
  await d.trigger("toggle");
  expect(w.text()).toContain("think first");
  useFeedStore().generation += 1;
  await flushPromises();
  expect(w.text()).toContain("think first");
});

test("another story starts afresh: its default is chosen when its own flow loads", async () => {
  const { w, router } = await open();
  api.storyFlow.mockResolvedValue({ ...RUNNING, key: "AIR-18" });
  api.storyJob.mockClear();
  await router.push("/workflows/AIR-18");
  await flushPromises();
  expect(api.storyJob.mock.calls.every((c) => c[0] === "AIR-18")).toBe(true); // never the old story's job under the new key
  expect(lastDetailCall()).toEqual(["AIR-18", "layer-02", "build"]);
  expect(w.get("[data-lane][data-node=layer-02] [data-job=build]").attributes("aria-pressed")).toBe("true");
});

const FAILED_FLOW_SAME = flow([...STAGES, layer("01", "Comment id store", "passed", ["passed", "passed", "passed"]), layer("02", "Comment edit", "failed", ["passed", "failed", "pending"]), layer("03", "More", "running", ["passed", "passed", "running"])]);
const pressed = (w: Awaited<ReturnType<typeof open>>["w"]) => w.findAll("[data-job][aria-pressed=true]").map((b) => `${b.attributes("data-node")}/${b.attributes("data-job")}`);

test("a pin on a failed default stays across a refresh that has a running job too", async () => {
  const { w } = await open();
  api.storyFlow.mockResolvedValue(FAILED_FLOW_SAME);
  useFeedStore().generation += 1;
  await flushPromises();
  expect(pressed(w)).toEqual(["layer-02/checks"]);
});

test("the last-passed fallback is not pinned: a later failure takes the view", async () => {
  const { w } = await open("/workflows/AIR-17", DONE);
  expect(pressed(w)).toEqual(["publish/publish"]);
  api.storyFlow.mockResolvedValue(FAILED);
  useFeedStore().generation += 1;
  await flushPromises();
  expect(pressed(w)).toEqual(["layer-02/checks"]);
});

test("a pinned job a refresh no longer has is dropped and the default is picked again", async () => {
  const { w } = await open();
  api.storyFlow.mockResolvedValue(DONE); // the layers were re-planned: layer 02 is gone
  useFeedStore().generation += 1;
  await flushPromises();
  expect(w.text()).not.toContain("Nothing has run yet.");
  expect(pressed(w)).toEqual(["publish/publish"]);
});

test("picking another job shows a loading line, not the previous job, until its detail lands", async () => {
  const { w } = await open();
  let land: (d: ReturnType<typeof detail>) => void = () => {};
  api.storyJob.mockImplementation(() => new Promise((r) => (land = r)));
  await w.get("[data-lane][data-node=layer-01] [data-job=tests]").trigger("click");
  await flushPromises();
  expect(w.text()).toContain("Loading…");
  expect(w.find("details[data-step]").exists()).toBe(false);
  land(detail({ node: "layer-01", job: job({ id: "tests", label: "Tests" }) }));
  await flushPromises();
  expect(w.text()).not.toContain("Loading…");
  expect(w.get("section[aria-label=Job] h2").text()).toBe("Layer 01 / Tests");
});

test("a live refresh of the job on show does not blank it", async () => {
  const { w } = await open();
  let land: (d: ReturnType<typeof detail>) => void = () => {};
  api.storyJob.mockImplementation(() => new Promise((r) => (land = r)));
  useFeedStore().generation += 1;
  await flushPromises();
  expect(w.text()).not.toContain("Loading…");
  expect(w.get("section[aria-label=Job] h2").text()).toBe("Layer 02 / checks");
  land(detail({ node: "layer-02", job: job({ id: "checks", label: "checks" }) }));
});

test("a live status line outside the job names what is on show", async () => {
  const { w } = await open();
  expect(w.get("[aria-live=polite]").text()).toBe("Layer 02 / checks: passed");
  await w.get("[data-lane][data-node=layer-01] [data-job=tests]").trigger("click");
  await flushPromises();
  expect(w.get("[aria-live=polite]").text()).toBe("Layer 01 / tests: passed");
});
