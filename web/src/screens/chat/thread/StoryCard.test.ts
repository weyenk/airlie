import { flushPromises, mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import type { WorkflowDef } from "@/api/workflow_defs";
import { ApiError } from "@/api";
import { message, openChat, ROW, startThread } from "@/test-utils/thread";
import StoryCard from "./StoryCard.vue";

const api = vi.hoisted(() => ({ repoNames: vi.fn() }));
vi.mock("@/api/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflows")>()), ...api }));
const defsApi = vi.hoisted(() => ({ listWorkflowDefs: vi.fn() }));
vi.mock("@/api/workflow_defs", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflow_defs")>()), ...defsApi }));
const sessions = vi.hoisted(() => ({ startStoryCard: vi.fn() }));
vi.mock("@/api/sessions", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/sessions")>()), ...sessions }));

const STORY = "Add a New chat button\n\nDone when a click empties it.";
const PLAIN = { ...ROW, id: "chat-2", kind: "plain" as const, repo: null, base_ref: null, branch: null };

const def = (name: string, extra: Partial<WorkflowDef> = {}): WorkflowDef => ({ name, type: "story", source: "file", problems: [], pr: null, used_by: [], ...extra });

async function open(row = ROW, repos: string[] = ["acme/api", "weyenk/other"]) {
  startThread([message("u1", "user", [{ type: "text", text: "a button" }]), message("a1", "assistant", [{ type: "text", text: STORY }])], row);
  api.repoNames.mockResolvedValue(repos);
  const turns = await openChat();
  if (row.id !== ROW.id) turns.chatFor(row);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/", component: { template: "<i/>" } },
      { path: "/workflows/build/:name", name: "builder", component: { template: "<i/>" } },
    ],
  });
  await router.push("/");
  const w = mount(StoryCard, { props: { sessionId: row.id, messageId: "a1", storyId: "story-1", text: STORY }, global: { plugins: [router] } });
  await flushPromises();
  return { w, turns, router };
}
const repoBox = (w: Awaited<ReturnType<typeof open>>["w"]) => w.get("input[aria-label=Repository]");
const textBox = (w: Awaited<ReturnType<typeof open>>["w"]) => w.get("textarea[aria-label='Story request']");

beforeEach(() => {
  localStorage.clear();
  defsApi.listWorkflowDefs.mockReset().mockResolvedValue([def("story"), def("bug-fix", { type: "bug" })]);
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

test("the workflow select offers the registry's workflows, starting on none", async () => {
  const { w } = await open();
  const select = w.get<HTMLSelectElement>("select[aria-label=Workflow]");
  expect(select.element.disabled).toBe(false);
  expect(select.findAll("option").map((o) => o.text())).toEqual(["No workflow selected", "story", "bug-fix"]);
  expect(select.element.value).toBe("");
  await select.setValue("bug-fix");
  expect(select.element.value).toBe("bug-fix");
});

test("Open builder goes to the builder, on the chosen workflow when there is one", async () => {
  const { w, router } = await open();
  const openBuilder = () => w.findAll("button").find((b) => b.text() === "Open builder")!;
  await openBuilder().trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/workflows/build/new");
  await router.push("/");
  await w.get("select[aria-label=Workflow]").setValue("story");
  await openBuilder().trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/workflows/build/story");
});

test("a failed workflow list is said and Start story still works without it", async () => {
  defsApi.listWorkflowDefs.mockRejectedValue(new Error("down"));
  const { w } = await open();
  expect(w.text()).toContain("Workflows did not load");
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(sessions.startStoryCard).toHaveBeenCalledWith("chat-1", "story-1", { text: STORY, repo: "acme/api" });
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
