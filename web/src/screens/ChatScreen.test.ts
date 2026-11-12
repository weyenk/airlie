import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory } from "vue-router";
import App from "@/App.vue";
import { createAppRouter } from "@/router";
import { stubWindowWidth, thread } from "@/test-utils/chat-panel";
import { screenServer } from "@/test-utils/chat-screen";
import type { ScreenServer } from "@/test-utils/chat-screen";
import { message } from "@/test-utils/thread";
import { openStream, streamResponse, textChunks, turnChunks } from "@/test-utils/ui-stream";

let server: ScreenServer;
const mounted: ReturnType<typeof mount>[] = [];

const FIRST = thread("c1", { title: "first chat", updated: 10 });
const REPO = thread("r1", { kind: "repository", title: "in a VM", repo: "acme/api", branch: "airlie/chat-abc", updated: 20 });

async function open(path = "/") {
  setActivePinia(createPinia());
  const router = createAppRouter(createMemoryHistory("/"));
  await router.push(path);
  const w = mount(App, { global: { plugins: [router] }, attachTo: document.body });
  mounted.push(w);
  await flushPromises();
  return { w, router };
}
const box = (w: Awaited<ReturnType<typeof open>>["w"]) => w.get("textarea");
async function say(w: Awaited<ReturnType<typeof open>>["w"], text: string) {
  await box(w).setValue(text);
  await w.get("form[aria-label='Message airlie']").trigger("submit");
  await flushPromises();
}
const turns = () => server.calls.filter((c) => c.url === "/v1/airlie/chat/turn");

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  stubWindowWidth(1280);
  server = screenServer({
    threads: [FIRST, REPO],
    pins: ["acme/api"],
    repos: ["acme/api"],
    messages: {
      c1: [message("m1", "user", [{ type: "text", text: "what is AIR-30" }]), message("m2", "assistant", [{ type: "text", text: "It is blocked." }])],
    },
    turn: () => streamResponse(turnChunks("a1", textChunks("t1", "An answer."))),
  });
});
afterEach(() => {
  for (const w of mounted.splice(0)) w.unmount();
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

test("slash opens the last chat of this browser, with its saved thread", async () => {
  localStorage.setItem("airlie.chat.last", "c1");
  const { w, router } = await open("/");
  expect(router.currentRoute.value.fullPath).toBe("/chat/c1");
  expect(w.text()).toContain("It is blocked.");
  expect(w.get("h1").text()).toBe("first chat");
  expect(w.find("[data-welcome]").exists()).toBe(false);
});

test("a new profile opens a fresh plain chat with the greeting", async () => {
  server.threads = [];
  const { w, router } = await open("/");
  expect(server.calls.find((c) => c.method === "POST" && c.url === "/v1/airlie/chat/threads")?.body).toMatchObject({ kind: "plain" });
  expect(router.currentRoute.value.fullPath).toBe("/chat/new-1");
  expect(w.get("[data-welcome]").text()).toContain("Hi, I'm airlie's assistant");
  expect(w.findAll("[data-quick]").map((b) => b.text())).toEqual(["What can you do?", "What's running right now?", "/story"]);
  expect(w.get("h1").text()).toBe("New chat");
});

test("with chats but none remembered, slash opens the newest", async () => {
  const { router } = await open("/");
  expect(router.currentRoute.value.fullPath).toBe("/chat/r1");
  expect(server.calls.some((c) => c.method === "POST")).toBe(false);
});

test("a remembered chat that is gone opens the newest instead, and is forgotten", async () => {
  localStorage.setItem("airlie.chat.last", "deleted-on-the-phone");
  const { w, router } = await open("/");
  await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe("/chat/r1"));
  expect(w.text()).not.toContain("This chat no longer exists.");
  expect(localStorage.getItem("airlie.chat.last")).toBe("r1");
});

test("the conversation log is busy while a reply streams, so a screen reader waits for the whole reply", async () => {
  const stream = openStream();
  server.turn = () => stream.response;
  const { w } = await open("/chat/c1");
  const log = () => w.get("[role=log]");
  expect(log().attributes("aria-busy")).toBe("false");
  await say(w, "hello");
  stream.push({ type: "start", messageId: "a1" }, { type: "start-step" }, ...textChunks("t1", "An"));
  await vi.waitFor(() => expect(log().attributes("aria-busy")).toBe("true"));
  stream.push({ type: "finish-step" }, { type: "finish", messageMetadata: {} });
  stream.end();
  await vi.waitFor(() => expect(log().attributes("aria-busy")).toBe("false"));
});

test("/story's reply is a story card; Start story starts it, and a reload shows the started job", async () => {
  const STORY = "Add a New chat button\n\nDone when a click empties it.";
  server.turn = (body) => {
    // the server saves the reply with its story part, as the turn route does
    server.messages.r1 = [
      message("u1", "user", [{ type: "text", text: "a new chat button" }]),
      message("a1", "assistant", [{ type: "text", text: STORY }, { type: "data-story", id: "story-1", data: { text: STORY } }]),
    ];
    expect(body.skill).toBe("story");
    return streamResponse(turnChunks("a1", [...textChunks("t1", STORY), { type: "data-story", id: "story-1", data: { text: STORY } }]));
  };
  const { w } = await open("/chat/r1");
  await say(w, "/story a new chat button");
  await vi.waitFor(() => expect(w.find("[data-story-card]").exists()).toBe(true));
  expect((w.get("input[aria-label=Repository]").element as HTMLInputElement).value).toBe("acme/api");
  await w.get("[data-start]").trigger("click");
  await flushPromises();
  expect(server.calls.find((c) => c.url === "/v1/airlie/chat/threads/r1/stories/story-1/start")?.body).toEqual({ text: STORY, repo: "acme/api" });
  expect(server.calls.some((c) => c.url === "/v1/airlie/stories")).toBe(false); // the chat starts it, once
  expect(w.find("[data-story-card]").exists()).toBe(false);
  expect(w.get("[data-card]").text()).toContain("api · story");
  w.unmount();

  const again = await open("/chat/r1"); // a reload reads the saved thread
  await vi.waitFor(() => expect(again.w.find("[data-card]").exists()).toBe(true));
  expect(again.w.find("[data-story-card]").exists()).toBe(false);
});

test("a chat that is gone says so and offers a new one", async () => {
  const { w } = await open("/chat/nope");
  expect(w.text()).toContain("This chat no longer exists.");
  expect(w.findAll("button").some((b) => b.text() === "Start a new chat")).toBe(true);
});

test("a quick reply sends it; /story fills the box instead", async () => {
  server.threads.push(thread("c9", { title: "empty", updated: 30 }));
  const { w } = await open("/chat/c9");
  await w.findAll("[data-quick]")[2].trigger("click");
  await flushPromises();
  expect((box(w).element as HTMLTextAreaElement).value).toBe("/story ");
  expect(turns()).toEqual([]);
  await w.findAll("[data-quick]")[1].trigger("click");
  await flushPromises();
  expect((turns()[0].body as { messages: { parts: { text: string }[] }[] }).messages[0].parts[0].text).toBe("What's running right now?");
  expect(w.text()).toContain("An answer.");
});

test("Enter sends and the reply streams into the thread; Shift+Enter is a new line", async () => {
  const { w } = await open("/chat/c1");
  await box(w).setValue("hello");
  await box(w).trigger("keydown", { key: "Enter", shiftKey: true });
  expect(turns()).toEqual([]);
  await box(w).trigger("keydown", { key: "Enter" });
  await flushPromises();
  expect(turns()).toHaveLength(1);
  expect(w.text()).toContain("hello");
  expect(w.text()).toContain("An answer.");
  expect((box(w).element as HTMLTextAreaElement).value).toBe("");
});

test("the top bar tabs and the phone tab bar reach board, workflows, metrics and admin from a chat", async () => {
  for (const width of [1280, 500]) {
    stubWindowWidth(width);
    const { w, router } = await open("/chat/c1");
    const bar = width > 640 ? "[data-test=tabs]" : "nav[data-test=tabbar]";
    expect(w.get(`${bar} a[aria-current=page]`).text()).toBe("Chat");
    const links = w.findAll(`${bar} a`);
    expect(links.map((a) => a.text())).toEqual(["Chat", "Board", "Workflows", "Metrics", "Admin"]);
    for (const [i, path] of [[1, "/board"], [2, "/workflows"], [3, "/metrics"], [4, "/admin"]] as const) {
      await links[i].trigger("click");
      await vi.waitFor(() => expect(router.currentRoute.value.path).toBe(path)); // the screen's chunk loads first
      await router.push("/chat/c1");
      await flushPromises();
    }
    w.unmount();
    mounted.pop();
  }
});

test("an old airlie chat thread appears as Legacy chat, read-only, and can be removed", async () => {
  sessionStorage.setItem(
    "airlie.chat",
    JSON.stringify([
      { role: "user", text: "an earlier question", sent: "an earlier question" },
      { role: "chat", text: "an earlier answer", streaming: false, thinking: false, action: "" },
    ]),
  );
  const { w, router } = await open("/chat/c1");
  await w.get("[data-test=legacy-row]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/chat/legacy");
  expect(w.get("h1").text()).toBe("Legacy chat");
  expect(w.text()).toContain("an earlier answer");
  expect(w.find("textarea").exists()).toBe(false);
  expect(w.get("[data-test=legacy-note]").text()).toContain("read-only");
  await w.get("[data-test=legacy-note] button").trigger("click");
  await flushPromises();
  expect(w.find("[data-test=legacy-row]").exists()).toBe(false);
  expect(sessionStorage.getItem("airlie.chat")).toBeNull();
  expect(router.currentRoute.value.fullPath).not.toBe("/chat/legacy");
});

test("opening another chat keeps the first one's stream running", async () => {
  const stream = openStream();
  server.turn = () => stream.response;
  const { w, router } = await open("/chat/c1");
  await say(w, "tell me a story");
  stream.push({ type: "start", messageId: "a1" }, { type: "start-step" }, { type: "text-start", id: "t1" }, { type: "text-delta", id: "t1", delta: "Once upon" });
  await vi.waitFor(() => expect(w.text()).toContain("Once upon"));

  await router.push("/chat/r1"); // another chat
  await flushPromises();
  expect(w.get("h1").text()).toBe("in a VM");
  expect(w.text()).not.toContain("Once upon");
  stream.push({ type: "text-delta", id: "t1", delta: " a time" }, { type: "text-end", id: "t1" }, { type: "finish-step" }, { type: "finish", messageMetadata: {} });
  stream.end();
  await flushPromises();

  await router.push("/chat/c1"); // back: the reply went on without it
  await vi.waitFor(() => expect(w.text()).toContain("Once upon a time"));
  expect(turns()).toHaveLength(1);
});

test("the composer footer shows the model and, in a repository chat, Ask before pushing", async () => {
  const { w } = await open("/chat/r1");
  expect(w.get("[data-test=composer-footer] [data-model]").text()).toBe("Gemma 4");
  expect(w.get("[data-test=composer-footer] [data-ask-first]").text()).toBe("Ask before pushing");
  expect(w.get("[data-test=chat-header]").text()).toContain("Gemma 4");
});

/** jsdom has no layout: give the thread's scroller a size and a position. */
function layout(w: Awaited<ReturnType<typeof open>>["w"], top: number) {
  const el = w.get("[data-test=thread-scroll]").element as HTMLElement;
  Object.defineProperty(el, "scrollHeight", { configurable: true, get: () => 1000 });
  Object.defineProperty(el, "clientHeight", { configurable: true, get: () => 400 });
  el.scrollTop = top;
  return el;
}

test("a new word follows the end only when the person was reading the end; their own message always does", async () => {
  const stream = openStream();
  server.turn = () => stream.response;
  const { w } = await open("/chat/c1");
  const el = layout(w, 100); // reading up the page, 500px from the end
  await say(w, "q");
  await vi.waitFor(() => expect(el.scrollTop).toBe(1000)); // my own message: to the end
  el.scrollTop = 100; // I scroll back up while it answers
  stream.push({ type: "start", messageId: "a1" }, { type: "text-start", id: "t1" }, { type: "text-delta", id: "t1", delta: "first" });
  await vi.waitFor(() => expect(w.text()).toContain("first"));
  stream.push({ type: "text-delta", id: "t1", delta: " second" });
  await vi.waitFor(() => expect(w.text()).toContain("first second"));
  expect(el.scrollTop).toBe(100); // the words did not drag me down
  stream.end();
  await flushPromises();
});
