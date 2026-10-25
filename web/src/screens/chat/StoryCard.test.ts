import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { ApiError } from "@/api";
import { useChatStore } from "@/stores/chat";
import StoryCard from "./StoryCard.vue";

const api = vi.hoisted(() => ({ startStory: vi.fn(), repoNames: vi.fn() }));
vi.mock("@/api/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflows")>()), ...api }));

const STORY = "Add a New chat button\n\nDone when a click empties it.";

async function open(repos: string[] = ["weyenk/airlie", "weyenk/other"]) {
  setActivePinia(createPinia());
  api.repoNames.mockResolvedValue(repos);
  const chat = useChatStore();
  chat.push({ role: "chat", text: STORY, action: "story" });
  const w = mount(StoryCard, { props: { item: chat.thread[0] } });
  await flushPromises();
  return { w, chat, item: chat.thread[0] };
}
const repoBox = (w: Awaited<ReturnType<typeof open>>["w"]) => w.get("input[aria-label=Repository]");

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  api.startStory.mockReset().mockResolvedValue({ job_id: "story-AIR-38", kind: "story" });
});

test("the reply is an editable story with a repository and Start story", async () => {
  const { w } = await open();
  expect((w.get("textarea[aria-label='Story request']").element as HTMLTextAreaElement).value).toBe(STORY);
  expect(w.text()).toContain("Start story");
  expect(w.text()).toContain("ready to edit");
});

test("Start story without a repository says to pick one and posts nothing", async () => {
  const { w, item } = await open([]);
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(w.get("[role=alert]").text()).toBe("Pick a repository first.");
  expect(api.startStory).not.toHaveBeenCalled();
  expect(item.started).toBeUndefined();
});

test("a repository that is neither owner/name nor a path says so and posts nothing", async () => {
  const { w } = await open();
  await repoBox(w).setValue("airlie");
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(w.get("[role=alert]").text()).toContain("owner/name");
  expect(api.startStory).not.toHaveBeenCalled();
});

test("the last repository used is the default", async () => {
  localStorage.setItem("airlie.repo", "weyenk/other");
  const { w } = await open();
  expect((repoBox(w).element as HTMLInputElement).value).toBe("weyenk/other");
});

test("a path stored by today's app is honoured, sent as it is, and not overwritten by the default", async () => {
  localStorage.setItem("airlie.repo", "/work/airlie");
  const { w } = await open();
  expect((repoBox(w).element as HTMLInputElement).value).toBe("/work/airlie");
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(api.startStory).toHaveBeenCalledExactlyOnceWith({ text: STORY, repo: "/work/airlie" });
  expect(localStorage.getItem("airlie.repo")).toBe("/work/airlie");
});

test("Start story posts the edited text and repository, and the item becomes a started job", async () => {
  const { w, item, chat } = await open();
  await w.get("textarea[aria-label='Story request']").setValue(`${STORY}\nEdited.`);
  expect(item.storyText).toBe(`${STORY}\nEdited.`);
  expect(JSON.parse(sessionStorage.getItem("airlie.chat")!)[0].storyText).toBe(`${STORY}\nEdited.`);
  await repoBox(w).setValue("weyenk/airlie");
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(api.startStory).toHaveBeenCalledExactlyOnceWith({ text: `${STORY}\nEdited.`, repo: "weyenk/airlie" });
  expect(item.started).toEqual({ job_id: "story-AIR-38", kind: "story", label: "airlie · story" });
  expect(JSON.parse(sessionStorage.getItem("airlie.chat")!)[0].started.job_id).toBe("story-AIR-38");
  expect(localStorage.getItem("airlie.repo")).toBe("weyenk/airlie");
  expect(chat.thread).toHaveLength(1);
});

test("an empty request posts nothing; a refused start shows why and can be tried again", async () => {
  const { w } = await open();
  await repoBox(w).setValue("weyenk/airlie");
  await w.get("textarea[aria-label='Story request']").setValue("  ");
  await w.get("[data-start]").trigger("click");
  expect(w.get("[role=alert]").text()).toBe("The story request is empty.");
  expect(api.startStory).not.toHaveBeenCalled();
  await w.get("textarea[aria-label='Story request']").setValue("x");
  api.startStory.mockRejectedValueOnce(new ApiError(409, "story AIR-3 is already running"));
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(w.get("[role=alert]").text()).toBe("story AIR-3 is already running");
  expect((w.get("[data-start]").element as HTMLButtonElement).disabled).toBe(false);
  api.startStory.mockRejectedValueOnce(new ApiError(401, "no"));
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(w.get("[role=alert]").text()).toBe("Sign in again, then start the story.");
});
