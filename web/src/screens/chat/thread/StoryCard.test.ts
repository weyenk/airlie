import { flushPromises, mount } from "@vue/test-utils";
import { ApiError } from "@/api";
import { message, openChat, ROW, startThread } from "@/test-utils/thread";
import { createMemoryHistory, createRouter } from "vue-router";
import StoryCard from "./StoryCard.vue";

const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/:rest(.*)*", component: { template: "<i/>" } }] });

const api = vi.hoisted(() => ({ startStory: vi.fn(), repoNames: vi.fn() }));
vi.mock("@/api/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflows")>()), ...api }));
const sessions = vi.hoisted(() => ({ saveStoryJob: vi.fn() }));
vi.mock("@/api/sessions", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/sessions")>()), ...sessions }));

vi.mock("@/stores/workflows", () => ({ useWorkflowsStore: () => ({ running: [] }) }));

const STORY = "Add a New chat button\n\nDone when a click empties it.";
const PLAIN = { ...ROW, id: "chat-2", kind: "plain" as const, repo: null, base_ref: null, branch: null };

async function open(row = ROW, repos: string[] = ["acme/api", "weyenk/other"]) {
  startThread([message("u1", "user", [{ type: "text", text: "a button" }]), message("a1", "assistant", [{ type: "text", text: STORY }])], row);
  api.repoNames.mockResolvedValue(repos);
  const turns = await openChat();
  if (row.id !== ROW.id) turns.chatFor(row);
  const w = mount(StoryCard, { props: { sessionId: row.id, messageId: "a1", storyId: "story-1", text: STORY }, global: { plugins: [router] } });
  await flushPromises();
  return { w, turns };
}
const repoBox = (w: Awaited<ReturnType<typeof open>>["w"]) => w.get("input[aria-label=Repository]");
const textBox = (w: Awaited<ReturnType<typeof open>>["w"]) => w.get("textarea[aria-label='Story request']");

beforeEach(() => {
  localStorage.clear();
  api.startStory.mockReset().mockResolvedValue({ job_id: "story-AIR-38", kind: "story" });
  sessions.saveStoryJob.mockReset().mockImplementation(async (_t: string, story: string, job: Record<string, unknown>) => ({
    type: "data-job",
    id: `${story}-job`,
    data: { story, ...job },
  }));
});
afterEach(() => vi.unstubAllGlobals());

test("the reply is an editable story request with a repository and Start story", async () => {
  const { w } = await open();
  expect((textBox(w).element as HTMLTextAreaElement).value).toBe(STORY);
  expect(w.text()).toContain("Start story");
  expect(w.text()).toContain("story request");
});

test("a repository chat's repository is the default", async () => {
  localStorage.setItem("airlie.repo", "weyenk/other");
  const { w } = await open();
  expect((repoBox(w).element as HTMLInputElement).value).toBe("acme/api");
});

test("a plain chat defaults to the repository last used here", async () => {
  localStorage.setItem("airlie.repo", "weyenk/other");
  const { w } = await open(PLAIN);
  expect((repoBox(w).element as HTMLInputElement).value).toBe("weyenk/other");
});

test("Start story without a repository says to pick one and starts nothing", async () => {
  const { w } = await open(PLAIN, []);
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(w.get("[role=alert]").text()).toBe("Pick a repository first.");
  expect(api.startStory).not.toHaveBeenCalled();
});

test("Start story starts the edited request, keeps its job on the message and shows it", async () => {
  const { w, turns } = await open();
  await textBox(w).setValue(`${STORY}\nEdited.`);
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(api.startStory).toHaveBeenCalledExactlyOnceWith({ text: `${STORY}\nEdited.`, repo: "acme/api" });
  expect(sessions.saveStoryJob).toHaveBeenCalledExactlyOnceWith(ROW.id, "story-1", { job_id: "story-AIR-38", kind: "story", label: "api · story" });
  const parts = turns.messagesOf(ROW.id)[1].parts as { type: string; data?: { job_id?: string } }[];
  expect(parts.at(-1)).toMatchObject({ type: "data-job", data: { job_id: "story-AIR-38" } });
});

test("a refused start shows why and can be tried again; a 401 says to sign in", async () => {
  const { w } = await open();
  api.startStory.mockRejectedValueOnce(new ApiError(409, "story AIR-3 is already running"));
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(w.get("[role=alert]").text()).toBe("story AIR-3 is already running");
  expect((w.get("[data-start]").element as HTMLButtonElement).disabled).toBe(false);
  api.startStory.mockRejectedValueOnce(new ApiError(401, "no"));
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(w.get("[role=alert]").text()).toBe("Sign in again, then start the story.");
  expect(sessions.saveStoryJob).not.toHaveBeenCalled();
});

test("a started story the chat could not keep still shows its job and says it was not kept", async () => {
  const { w, turns } = await open();
  sessions.saveStoryJob.mockRejectedValueOnce(new ApiError(500, "boom"));
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(turns.messagesOf(ROW.id)[1].parts.map((p) => p.type)).toEqual(["text"]);
  expect(w.find("[data-card]").exists()).toBe(true);
  expect(w.find("[data-start]").exists()).toBe(false); // started once: no second Start
  expect(w.get("[role=alert]").text()).toBe("The story started, but this chat could not keep it: boom");
});
