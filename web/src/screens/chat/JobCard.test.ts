import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { ApiError } from "@/api";
import { useFeedStore } from "@/stores/feed";
import JobCard from "./JobCard.vue";

const api = vi.hoisted(() => ({ listRunning: vi.fn(), storyFlow: vi.fn() }));
vi.mock("@/api/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflows")>()), ...api }));

const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:rest(.*)*", component: { template: "<i/>" } }] });
async function show(job: object) {
  setActivePinia(createPinia());
  const w = mount(JobCard, { props: { job: job as never }, global: { plugins: [router] } });
  await flushPromises();
  return w;
}
const story = { job_id: "story-AIR-38", kind: "story", label: "airlie · story" };

beforeEach(() => {
  api.listRunning.mockReset().mockResolvedValue([]);
  api.storyFlow.mockReset().mockRejectedValue(new ApiError(404, "no story"));
});

test("a story job shows its label, its stage and progress while it runs, and opens its workflow", async () => {
  api.listRunning.mockResolvedValue([{ kind: "story", key: "AIR-38", title: "t", topology: null, progress: 0.12, last_activity: 1 }]);
  api.storyFlow.mockResolvedValue({ key: "AIR-38", stage: "intake", blocked: null });
  const w = await show(story);
  expect(w.get("[data-kind]").text()).toBe("story started");
  expect(w.text()).toContain("airlie · story");
  expect(w.get("[data-stage]").text()).toBe("refining");
  expect(w.get("[role=progressbar]").attributes("aria-valuenow")).toBe("12");
  const link = w.get("a[data-card-link]");
  expect(link.text()).toContain("Open workflow");
  expect(link.attributes("href")).toBe("/workflows/AIR-38");
});

test("a story that is not running has no progress bar and says what stage it reached", async () => {
  api.storyFlow.mockResolvedValue({ key: "AIR-38", stage: "publish", blocked: null });
  const w = await show(story);
  expect(w.find("[role=progressbar]").exists()).toBe(false);
  expect(w.get("[data-stage]").text()).toBe("in review");
});

test("a story with no state yet says so, or is starting when it already runs; a blocked one says blocked", async () => {
  expect((await show(story)).get("[data-stage]").text()).toBe("no state yet");
  api.listRunning.mockResolvedValueOnce([{ kind: "story", key: "AIR-38", title: null, topology: null, progress: null, last_activity: 1 }]);
  expect((await show(story)).get("[data-stage]").text()).toBe("starting");
  api.storyFlow.mockResolvedValue({ key: "AIR-38", stage: "layers", blocked: "gate" });
  expect((await show(story)).get("[data-stage]").text()).toBe("blocked");
});

test("a run or a plan goes to its run, and never looks up a story", async () => {
  const w = await show({ job_id: "run-7", kind: "run", label: "l" });
  expect(w.get("[data-kind]").text()).toBe("job started");
  expect(w.get("a[data-card-link]").attributes("href")).toBe("/workflows/runs/run-7");
  expect(api.storyFlow).not.toHaveBeenCalled();
  expect((await show({ job_id: "plan-1", kind: "plan" })).get("[data-kind]").text()).toBe("planning");
});

test("the story is looked up by its exact key, so AIR-1 is never mistaken for AIR-10 to AIR-15", async () => {
  api.storyFlow.mockImplementation(async (k: string) => ({ key: k, stage: k === "AIR-1" ? "layers" : "review", blocked: null }));
  const w = await show({ job_id: "story-AIR-1", kind: "story", label: "l" });
  expect(api.storyFlow).toHaveBeenCalledExactlyOnceWith("AIR-1");
  expect(w.get("[data-stage]").text()).toBe("building");
});

test("a published story is not asked about again when stories change", async () => {
  api.storyFlow.mockResolvedValue({ key: "AIR-38", stage: "review", blocked: null });
  setActivePinia(createPinia());
  const feed = useFeedStore();
  const w = mount(JobCard, { props: { job: story as never }, global: { plugins: [router] } });
  await flushPromises();
  expect(w.get("[data-stage]").text()).toBe("in review");
  const asked = api.storyFlow.mock.calls.length;
  feed.generation += 1; // the feed reconnects: every live card refetches
  await flushPromises();
  expect(api.storyFlow.mock.calls.length).toBe(asked);
});
