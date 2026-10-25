import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import ChatScreen from "./ChatScreen.vue";

const chatApi = vi.hoisted(() => ({ stream: vi.fn(), chatSkills: vi.fn(), attachmentLimits: vi.fn() }));
vi.mock("@/api/chat", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/chat")>()), ...chatApi }));
vi.mock("./chat/StatusStrip.vue", () => ({ default: { template: "<div data-test='strip-stub' />" } }));

async function open() {
  setActivePinia(createPinia());
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: ChatScreen }] });
  await router.push("/");
  const w = mount(ChatScreen, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return w;
}
const type = async (w: Awaited<ReturnType<typeof open>>, text: string) => w.get("textarea").setValue(text);
const press = async (w: Awaited<ReturnType<typeof open>>, init: KeyboardEventInit) => {
  await w.get("textarea").trigger("keydown", init);
  await flushPromises();
};

beforeEach(() => {
  sessionStorage.clear();
  chatApi.stream.mockReset().mockImplementation(async (_p: string, _b: unknown, onDelta: (t: string) => void) => {
    onDelta("An answer.");
    return "An answer.";
  });
  chatApi.attachmentLimits.mockReset().mockResolvedValue({ max_per_message: 10, max_image_mb: 20, max_video_mb: 100, max_file_mb: 25 });
  chatApi.chatSkills.mockReset().mockResolvedValue([
    { name: "story", summary: "s", action: "story" },
    { name: "clear", summary: "c", action: "clear" },
  ]);
});
afterEach(() => {
  document.body.innerHTML = "";
});

test("a new thread greets and offers three quick replies", async () => {
  const w = await open();
  expect(w.get("[data-welcome]").text()).toContain("Hi, I'm airlie's assistant");
  expect(w.findAll("[data-quick]").map((b) => b.text())).toEqual(["What can you do?", "What's running right now?", "/story"]);
});

test("a quick reply asks the model; /story fills the box instead", async () => {
  const w = await open();
  await w.findAll("[data-quick]")[1].trigger("click");
  await flushPromises();
  expect(chatApi.stream.mock.calls[0][1].messages).toEqual([{ role: "user", content: "What's running right now?" }]);
  expect(w.find("[data-welcome]").exists()).toBe(false);
});

test("the /story quick reply puts /story and a space in the box and sends nothing", async () => {
  const w = await open();
  await w.findAll("[data-quick]")[2].trigger("click");
  await flushPromises();
  expect((w.get("textarea").element as HTMLTextAreaElement).value).toBe("/story ");
  expect(chatApi.stream).not.toHaveBeenCalled();
});

test("Enter sends; Shift+Enter is a new line", async () => {
  const w = await open();
  await type(w, "hello");
  await press(w, { key: "Enter", shiftKey: true });
  expect(chatApi.stream).not.toHaveBeenCalled();
  await press(w, { key: "Enter" });
  expect(chatApi.stream).toHaveBeenCalledTimes(1);
  expect(w.text()).toContain("An answer.");
  expect((w.get("textarea").element as HTMLTextAreaElement).value).toBe("");
});

test("the Send button sends, and an empty box sends nothing", async () => {
  const w = await open();
  await w.get("button[aria-label=Send]").trigger("click");
  expect(chatApi.stream).not.toHaveBeenCalled();
  await type(w, "  ");
  await w.get("form").trigger("submit");
  expect(chatApi.stream).not.toHaveBeenCalled();
  await type(w, "hi");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(chatApi.stream).toHaveBeenCalledTimes(1);
});

test("/clear starts a fresh session without asking the model", async () => {
  const w = await open();
  await type(w, "hello");
  await press(w, { key: "Enter" });
  expect(w.findAll("[data-msg=user]")).toHaveLength(1);
  chatApi.stream.mockClear();
  await type(w, "/clear");
  await press(w, { key: "Enter" });
  expect(w.findAll("[data-msg=user]")).toHaveLength(0);
  expect(w.find("[data-welcome]").exists()).toBe(true);
  expect(chatApi.stream).not.toHaveBeenCalled();
});

test("a skill command sends the skill and its request; a bare one asks what for", async () => {
  const w = await open();
  await type(w, "/story");
  await press(w, { key: "Enter" });
  expect(chatApi.stream).not.toHaveBeenCalled();
  await type(w, "/story a button");
  await press(w, { key: "Enter" });
  expect(chatApi.stream.mock.calls[0][1]).toMatchObject({ skill: "story", messages: [{ role: "user", content: "a button" }] });
});

test("the draft is kept under airlie.chat.draft and comes back", async () => {
  const w = await open();
  await type(w, "half");
  expect(JSON.parse(sessionStorage.getItem("airlie.chat.draft")!)).toBe("half");
  w.unmount();
  const again = await open();
  expect((again.get("textarea").element as HTMLTextAreaElement).value).toBe("half");
});

/** jsdom has no layout: give the thread's scroller a size and a position. */
function layout(w: Awaited<ReturnType<typeof open>>, top: number) {
  const el = w.get("[data-test=thread-scroll]").element as HTMLElement;
  Object.defineProperty(el, "scrollHeight", { configurable: true, get: () => 1000 });
  Object.defineProperty(el, "clientHeight", { configurable: true, get: () => 400 });
  el.scrollTop = top;
  return el;
}

test("a new word follows the end only when the person was reading the end; their own message always does", async () => {
  chatApi.stream.mockImplementation(async (_p: string, _b: unknown, onDelta: (t: string) => void) => {
    await new Promise((r) => setTimeout(r, 0));
    onDelta("first");
    await new Promise((r) => setTimeout(r, 0));
    onDelta(" second");
    return "";
  });
  const w = await open();
  const el = layout(w, 100); // reading up the page, 500px from the end
  await type(w, "q");
  const sending = press(w, { key: "Enter" });
  await flushPromises();
  expect(el.scrollTop).toBe(1000); // my own message: to the end
  el.scrollTop = 100; // I scroll back up while it answers
  await new Promise((r) => setTimeout(r, 5));
  await sending;
  expect(el.scrollTop).toBe(100); // the words did not drag me down
});
