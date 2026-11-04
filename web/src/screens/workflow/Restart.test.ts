import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { computed } from "vue";
import { createMemoryHistory, createRouter } from "vue-router";
import { ApiError } from "@/api";
import type { Flow, FlowCard, JobRow } from "@/api/workflows";
import { answerConfirm } from "@/composables/useConfirm";
import { detail, flow, job, layer, now, stage } from "@/test-utils/workflow-fixtures";
import WorkflowScreen from "../WorkflowScreen.vue";
import JobHover from "./JobHover.vue";
import { RESTART, restartKeeps, restartPoint } from "./restart";

const api = vi.hoisted(() => ({
  storyFlow: vi.fn(),
  storyJob: vi.fn(),
  listJobs: vi.fn(),
  listRunning: vi.fn(),
  cancelJob: vi.fn(),
  restartStory: vi.fn(),
}));
vi.mock("@/api/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflows")>()), ...api }));

const STAGES = [stage("intake", "Intake"), stage("refine", "Refine"), stage("layers", "Plan layers"), stage("packages", "Draft packages")];
const STOPPED = flow([...STAGES, layer("01", "Comment id store", "passed", ["passed", "passed", "passed"]), layer("02", "Comment edit", "failed", ["passed", "failed", "pending"]), layer("03", "Comment list", "pending", ["pending", "pending", "pending"]), stage("publish", "Publish", "pending")], {
  blocked: "layer 02: its tests still do not test its change",
});

function phone(on: boolean) {
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: on, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
}

async function open(url: string, f: Flow = STOPPED) {
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
  return w;
}
const inSheet = (selector: string) => document.body.querySelector(`[data-sheet] ${selector}`) as HTMLButtonElement | null;
const dialog = () => document.body.querySelector("[data-restart-dialog]");
const running = (): JobRow => ({ id: "story-AIR-17", kind: "story", state: "running", started: 1 });

beforeEach(() => {
  setActivePinia(createPinia());
  api.storyJob.mockReset().mockImplementation(async (_k: string, node: string, jobId: string) => detail({ node, job: job({ id: jobId, label: jobId }) }));
  api.listJobs.mockReset().mockResolvedValue([]);
  api.listRunning.mockReset().mockResolvedValue([]);
  api.cancelJob.mockReset();
  api.restartStory.mockReset();
});
afterEach(() => {
  document.body.innerHTML = "";
  answerConfirm(false);
  vi.unstubAllGlobals();
});

test("each step restarts from its own point; a step not reached, and the steps after the build, from none", () => {
  const point = (id: string, status: "passed" | "pending" = "passed") => restartPoint({ ...stage(id, id, status), id });
  expect(["intake", "base", "gate", "survey", "refine"].map((id) => point(id))).toEqual(Array(5).fill("refine"));
  expect(["layers", "amigos", "packages", "layer-03"].map((id) => point(id))).toEqual(["layers", "amigos", "packages", "layer-03"]);
  expect(["planned", "publish", "review", "exit"].map((id) => point(id))).toEqual([null, null, null, null]);
  expect(point("layer-03", "pending")).toBeNull();
  expect(restartKeeps("layer-03")).toContain("layers 01 to 02, replayed on the latest main");
  expect(restartKeeps("refine")).toContain("Nothing");
});

describe("on a laptop, in a step's hover card", () => {
  const card: FlowCard = { title: "Build", tags: [], rows: [], foot: null };
  async function hover(pointOf: (id: string) => string | null, c: FlowCard | null = card, busy = false) {
    vi.useFakeTimers();
    const ask = vi.fn();
    const w = mount(JobHover, {
      props: { card: c, nodeId: "layer-02" },
      slots: { default: "<button type='button'>box</button>" },
      global: { provide: { [RESTART as symbol]: { pointOf, running: computed(() => busy), ask } } },
      attachTo: document.body,
    });
    await w.find("button").trigger("focus");
    vi.advanceTimersByTime(300);
    vi.useRealTimers();
    await flushPromises();
    return { w, ask, button: document.body.querySelector("[data-restart-button]") as HTMLButtonElement | null };
  }

  test("a reached step's card offers Restart from here, which asks for the step", async () => {
    const { w, ask, button } = await hover(() => "layer-02");
    expect(document.body.querySelector("[data-card]")).not.toBeNull();
    expect(button?.textContent).toContain("Restart from here");
    expect(button?.dataset.point).toBe("layer-02");
    button!.click();
    expect(ask).toHaveBeenCalledWith("layer-02");
    w.unmount();
  });

  test("a step not reached has no Restart from here", async () => {
    const { w, button } = await hover(() => null);
    expect(document.body.querySelector("[data-card]")).not.toBeNull();
    expect(button).toBeNull();
    w.unmount();
  });

  test("while the story runs the action is disabled and says to stop it first", async () => {
    const { w, button } = await hover(() => "layer-02", card, true);
    expect(button?.disabled).toBe(true);
    expect(document.body.querySelector("[data-restart]")?.textContent).toContain("stop it first");
    w.unmount();
  });
});

describe("on a phone, in a step's sheet", () => {
  beforeEach(() => phone(true));

  test("a reached step's sheet confirms what is kept and discarded, then restarts it on the latest main", async () => {
    api.restartStory.mockResolvedValue({ key: "AIR-17", point: "layer-02", closed: [], branches: ["b2"], worktrees: [], archive: "/a", note: "started over from layer-02 on origin/main @ abc" });
    await open("/workflows/AIR-17/layer-02/build");
    const button = inSheet("[data-restart-button]");
    expect(button?.dataset.point).toBe("layer-02");
    button!.click();
    await flushPromises();
    expect(dialog()?.textContent).toContain("Restart AIR-17 from Layer 02?");
    expect(dialog()?.querySelector("[data-kept]")?.textContent).toContain("layers 01 to 01, replayed on the latest main");
    expect(dialog()?.querySelector("[data-discarded]")?.textContent).toMatch(/pull requests are closed.*branches.*archived/s);
    expect(dialog()?.textContent).toContain("moves onto the latest main");
    expect(api.restartStory).not.toHaveBeenCalled();
    (dialog()!.querySelector("[data-restart-ok]") as HTMLButtonElement).click();
    await flushPromises();
    expect(api.restartStory).toHaveBeenCalledWith("AIR-17", "layer-02");
    expect(dialog()).toBeNull();
    expect(document.body.querySelector("[data-restarted]")?.textContent).toContain("started over from layer-02");
    expect(api.storyFlow.mock.calls.length).toBeGreaterThan(1); // the flow is read again
  });

  test("the server's refusal is shown in the dialog as it says it", async () => {
    api.restartStory.mockRejectedValue(new ApiError(422, "layer 01 conflicts with the trunk; start the story over from an earlier step"));
    await open("/workflows/AIR-17/refine/refine");
    expect(inSheet("[data-restart-button]")?.dataset.point).toBe("refine");
    inSheet("[data-restart-button]")!.click();
    await flushPromises();
    expect(dialog()?.querySelector("[data-kept]")?.textContent).toContain("Nothing");
    (dialog()!.querySelector("[data-restart-ok]") as HTMLButtonElement).click();
    await flushPromises();
    expect(dialog()?.querySelector("[data-restart-error]")?.textContent).toContain("conflicts with the trunk; start the story over from an earlier step");
    expect(document.body.querySelector("[data-restarted]")).toBeNull();
  });

  test("a step after the build, or one not reached, has no restart", async () => {
    await open("/workflows/AIR-17/publish/publish");
    expect(document.body.querySelector("[data-sheet]")).not.toBeNull();
    expect(inSheet("[data-restart-button]")).toBeNull();
  });

  test("while this server runs the story, or its flow says it runs, the action is disabled", async () => {
    api.listJobs.mockResolvedValue([running()]);
    await open("/workflows/AIR-17/layers/layers");
    expect(inSheet("[data-restart-button]")?.disabled).toBe(true);
    expect(inSheet("[data-restart]")?.textContent).toContain("stop it first");
    document.body.innerHTML = "";
    api.listJobs.mockResolvedValue([]);
    await open("/workflows/AIR-17/layers/layers", { ...STOPPED, now: now({ status: "running" }) });
    expect(inSheet("[data-restart-button]")?.disabled).toBe(true);
  });
});
