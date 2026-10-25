import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { previews } from "./attachments";
import Bubble from "./Bubble.vue";

const chatApi = vi.hoisted(() => ({ uploadUrl: vi.fn() }));
vi.mock("@/api/chat", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/chat")>()), ...chatApi }));
const wf = vi.hoisted(() => ({ listRunning: vi.fn(), listStories: vi.fn(), repoNames: vi.fn() }));
vi.mock("@/api/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflows")>()), ...wf }));

beforeEach(() => {
  setActivePinia(createPinia());
  chatApi.uploadUrl.mockReset().mockRejectedValue(new Error("gone"));
  wf.listRunning.mockReset().mockResolvedValue([]);
  wf.listStories.mockReset().mockResolvedValue({ stories: [], total: 0, page: 1, size: 5 });
  wf.repoNames.mockReset().mockResolvedValue([]);
});

const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:rest(.*)*", component: { template: "<i/>" } }] });
const show = (item: object) => mount(Bubble, { props: { item: item as never }, global: { plugins: [router] } });

test("a reply with no words yet says Thinking, and the words replace it", async () => {
  const w = show({ role: "chat", text: "", streaming: true });
  expect(w.get("[data-thinking]").text()).toBe("Thinking");
  await w.setProps({ item: { role: "chat", text: "Two things", streaming: true } });
  expect(w.find("[data-thinking]").exists()).toBe(false);
  expect(w.text()).toContain("Two things");
});

test("a reply is markdown with story keys as links, and its HTML is only text", () => {
  const w = show({ role: "chat", text: "**AIR-30** is blocked <script>alert(1)</script> <img src=x onerror=alert(1)>" });
  expect(w.get("a[href='/workflows/AIR-30']").text()).toBe("AIR-30");
  expect(w.find("script").exists()).toBe(false);
  expect(w.find("img").exists()).toBe(false);
  expect(w.text()).toContain("<script>alert(1)</script>");
});

test("a cut reply keeps its text and says airlie stopped answering", () => {
  const w = show({ role: "chat", text: "half an ans", cut: true });
  expect(w.text()).toContain("half an ans");
  expect(w.get("[data-cut]").text()).toBe("airlie stopped answering");
});

test("a failed reply shows its error", () => {
  expect(show({ role: "chat", text: "", error: "Can't reach airlie: boom" }).get("[data-error]").text()).toBe("Can't reach airlie: boom");
});

test("my message is plain text with the names of what I attached", () => {
  const w = show({ role: "user", text: "<b>hi</b>", attachments: [{ id: "u1", name: "notes.log", size: 1, kind: "file" }] });
  expect(w.text()).toContain("<b>hi</b>");
  expect(w.find("b").exists()).toBe(false);
  expect(w.get("[data-attachment]").text()).toBe("notes.log · 1 B");
});

test("a picture I attached is the picture, fetched again when this page has not got it", async () => {
  previews.clear();
  chatApi.uploadUrl.mockResolvedValueOnce("blob:again");
  const w = show({ role: "user", text: "see", attachments: [{ id: "u9", name: "shot.png", size: 1, kind: "image" }] });
  await flushPromises();
  expect(chatApi.uploadUrl).toHaveBeenCalledWith("u9");
  expect(w.get("img[data-attachment]").attributes("src")).toBe("blob:again");
});

test("a picture that is gone says so", async () => {
  previews.clear();
  chatApi.uploadUrl.mockRejectedValueOnce(new Error("404"));
  const w = show({ role: "user", text: "see", attachments: [{ id: "u8", name: "old.png", size: 2048, kind: "image" }] });
  await flushPromises();
  expect(w.get("[data-attachment]").text()).toBe("old.png · 2 KB (no longer available)");
});

test("a job card from today's app is the job card, and goes to its story", () => {
  const w = show({ role: "card", job_id: "story-AIR-30", kind: "story", label: "airlie · story" });
  expect(w.get("[data-card] [data-kind]").text()).toBe("story started");
  expect(w.get("[data-card]").text()).toContain("airlie · story");
  expect(w.get("[data-card-link]").attributes("href")).toBe("/workflows/AIR-30");
});

test("a card with no label names its job", () => {
  const w = show({ role: "card", job_id: "run-7", kind: "run" });
  expect(w.get("[data-card] [data-kind]").text()).toBe("job started");
  expect(w.get("[data-card]").text()).toContain("run-7");
});

test("a plan's or a run's card goes to its run, a story's to its key", () => {
  expect(show({ role: "card", job_id: "run-7", kind: "run" }).get("[data-card-link]").attributes("href")).toBe("/workflows/runs/run-7");
  expect(show({ role: "card", job_id: "plan-3", kind: "plan" }).get("[data-card-link]").attributes("href")).toBe("/workflows/runs/plan-3");
});

test("a /story reply is the story card; a started one is the job card; a cut or failed one stays a bubble", () => {
  const story = { role: "chat", text: "Add a thing", action: "story", streaming: false };
  expect(show(story).find("[data-story-card]").exists()).toBe(true);
  expect(show({ ...story, streaming: true }).find("[data-story-card]").exists()).toBe(false);
  expect(show({ ...story, cut: true }).find("[data-story-card]").exists()).toBe(false);
  expect(show({ ...story, error: "x" }).find("[data-story-card]").exists()).toBe(false);
  const started = show({ ...story, started: { job_id: "story-AIR-38", kind: "story", label: "airlie · story" } });
  expect(started.find("[data-story-card]").exists()).toBe(false);
  expect(started.get("[data-card-link]").attributes("href")).toBe("/workflows/AIR-38");
});
