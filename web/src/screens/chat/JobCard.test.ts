import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import JobCard from "./JobCard.vue";

const api = vi.hoisted(() => ({ listRunning: vi.fn(), listStories: vi.fn() }));
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
  api.listStories.mockReset().mockResolvedValue({ stories: [], total: 0, page: 1, size: 5 });
});

test("a story job shows its label, its stage and progress while it runs, and opens its workflow", async () => {
  api.listRunning.mockResolvedValue([{ kind: "story", key: "AIR-38", title: "t", topology: null, progress: 0.12, last_activity: 1 }]);
  api.listStories.mockResolvedValue({ stories: [{ key: "AIR-38", title: "t", repo: "a/b", stage: "intake", blocked: null, last_activity: 1 }], total: 1, page: 1, size: 5 });
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
  api.listStories.mockResolvedValue({ stories: [{ key: "AIR-38", title: "t", repo: "a/b", stage: "publish", blocked: null, last_activity: 1 }], total: 1, page: 1, size: 5 });
  const w = await show(story);
  expect(w.find("[role=progressbar]").exists()).toBe(false);
  expect(w.get("[data-stage]").text()).toBe("in review");
});

test("a story with no state yet is starting; a blocked one says blocked", async () => {
  expect((await show(story)).get("[data-stage]").text()).toBe("starting");
  api.listStories.mockResolvedValue({ stories: [{ key: "AIR-38", title: "t", repo: "a/b", stage: "layers", blocked: "gate", last_activity: 1 }], total: 1, page: 1, size: 5 });
  expect((await show(story)).get("[data-stage]").text()).toBe("blocked");
});

test("a run or a plan goes to its run, and never looks up a story", async () => {
  const w = await show({ job_id: "run-7", kind: "run", label: "l" });
  expect(w.get("[data-kind]").text()).toBe("job started");
  expect(w.get("a[data-card-link]").attributes("href")).toBe("/workflows/runs/run-7");
  expect(api.listStories).not.toHaveBeenCalled();
  expect((await show({ job_id: "plan-1", kind: "plan" })).get("[data-kind]").text()).toBe("planning");
});
