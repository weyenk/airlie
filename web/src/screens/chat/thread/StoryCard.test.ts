import { flushPromises, mount } from "@vue/test-utils";
import { ApiError } from "@/api";
import { message, openChat, ROW, startThread } from "@/test-utils/thread";
import StoryCard from "./StoryCard.vue";

const api = vi.hoisted(() => ({ repoNames: vi.fn() }));
vi.mock("@/api/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflows")>()), ...api }));
const sessions = vi.hoisted(() => ({ startStoryCard: vi.fn() }));
vi.mock("@/api/sessions", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/sessions")>()), ...sessions }));

const STORY = "Add a New chat button\n\nDone when a click empties it.";
const PLAIN = { ...ROW, id: "chat-2", kind: "plain" as const, repo: null, base_ref: null, branch: null };

async function open(row = ROW, repos: string[] = ["acme/api", "weyenk/other"]) {
  startThread([message("u1", "user", [{ type: "text", text: "a button" }]), message("a1", "assistant", [{ type: "text", text: STORY }])], row);
  api.repoNames.mockResolvedValue(repos);
  const turns = await openChat();
  if (row.id !== ROW.id) turns.chatFor(row);
  const w = mount(StoryCard, { props: { sessionId: row.id, messageId: "a1", storyId: "story-1", text: STORY } });
  await flushPromises();
  return { w, turns };
}
const repoBox = (w: Awaited<ReturnType<typeof open>>["w"]) => w.get("input[aria-label=Repository]");
const textBox = (w: Awaited<ReturnType<typeof open>>["w"]) => w.get("textarea[aria-label='Story request']");

beforeEach(() => {
  localStorage.clear();
  sessions.startStoryCard.mockReset().mockImplementation(async (_t: string, story: string, body: { repo: string }) => ({
    type: "data-job",
    id: `${story}-job`,
    data: { story, job_id: "story-AIR-38", kind: "story", label: `${body.repo.split("/").pop()} · story` },
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
  expect(sessions.startStoryCard).not.toHaveBeenCalled();
});

test("Start story asks the chat to start the edited request, and the job it kept shows on the message", async () => {
  const { w, turns } = await open();
  await textBox(w).setValue(`${STORY}\nEdited.`);
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(sessions.startStoryCard).toHaveBeenCalledExactlyOnceWith(ROW.id, "story-1", { text: `${STORY}\nEdited.`, repo: "acme/api" });
  const parts = turns.messagesOf(ROW.id)[1].parts as { type: string; data?: { job_id?: string } }[];
  expect(parts.at(-1)).toMatchObject({ type: "data-job", data: { job_id: "story-AIR-38" } });
});

test("a refused start shows why and can be tried again; a 401 says to sign in", async () => {
  const { w, turns } = await open();
  sessions.startStoryCard.mockRejectedValueOnce(new ApiError(409, "story AIR-3 is already running"));
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(w.get("[role=alert]").text()).toBe("story AIR-3 is already running");
  expect((w.get("[data-start]").element as HTMLButtonElement).disabled).toBe(false);
  sessions.startStoryCard.mockRejectedValueOnce(new ApiError(401, "no"));
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(w.get("[role=alert]").text()).toBe("Sign in again, then start the story.");
  expect(turns.messagesOf(ROW.id)[1].parts.map((p) => p.type)).toEqual(["text"]);
});
