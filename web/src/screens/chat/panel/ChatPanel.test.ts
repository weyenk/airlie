import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import ChatPanel from "./ChatPanel.vue";
import type { Change, FeedState } from "@/feed";
import { useFeedStore } from "@/stores/feed";
import { panelServer, thread } from "@/test-utils/chat-panel";

const feed = vi.hoisted(() => ({ opts: undefined as { onChange(c: Change): void; onState(s: FeedState): void } | undefined }));
vi.mock("@/feed", () => ({
  connectFeed: (opts: { onChange(c: Change): void; onState(s: FeedState): void }) => {
    feed.opts = opts;
    return () => {};
  },
}));

const Blank = { template: "<div />" };

function router() {
  return createRouter({ history: createMemoryHistory(), routes: [{ path: "/:rest(.*)*", component: Blank }] });
}

async function open(currentId: string | null = null) {
  setActivePinia(createPinia());
  useFeedStore().start();
  const r = router();
  const w = mount(ChatPanel, { props: { currentId }, global: { plugins: [r] }, attachTo: document.body }); // a form submits only when it is in the document
  await flushPromises();
  return { w, r };
}

afterEach(() => {
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

const REPO = (id: string, repo: string, updated: number, title = "") =>
  thread(id, { kind: "repository", repo, branch: `airlie/chat-${id}`, title, updated });

const button = (w: Awaited<ReturnType<typeof open>>["w"], name: string) => w.findAll("button").find((b) => b.attributes("aria-label") === name || b.text() === name)!;
const rowTitles = (w: Awaited<ReturnType<typeof open>>["w"], group: string) =>
  w.get(`section[aria-label="${group}"]`).findAll("button:not([aria-label]) > span:first-child").map((t) => t.text());

test("each pinned repository lists its chats, newest first", async () => {
  panelServer({
    pins: ["acme/api", "acme/web"],
    threads: [REPO("a", "acme/api", 10, "older"), REPO("b", "acme/api", 20, "newer"), REPO("c", "acme/web", 5, "web chat")],
  });
  const { w } = await open();
  expect(w.findAll("h3").map((h) => h.text()).slice(0, 2)).toEqual(["acme/api", "acme/web"]);
  expect(rowTitles(w, "acme/api")).toEqual(["newer", "older"]);
  expect(rowTitles(w, "acme/web")).toEqual(["web chat"]);
});

test("a pinned repository with no chats yet is still listed", async () => {
  panelServer({ pins: ["acme/api"], threads: [thread("p1", { title: "plain" })] });
  const { w } = await open();
  expect(w.find('section[aria-label="acme/api"]').exists()).toBe(true);
});

test("new under a repository makes a repository chat and opens it", async () => {
  const server = panelServer({ pins: ["acme/api"] });
  const { w, r } = await open();
  await button(w, "New chat in acme/api").trigger("click");
  await flushPromises();
  expect(server.calls).toContainEqual({ method: "POST", url: "/v1/airlie/chat/threads", body: { kind: "repository", repo: "acme/api" } });
  expect(r.currentRoute.value.fullPath).toBe("/chat/new-1");
  expect(w.emitted("pick")).toEqual([["new-1"]]);
});

test("new under chats makes a plain chat and opens it", async () => {
  const server = panelServer();
  const { w, r } = await open();
  await button(w, "New chat").trigger("click");
  await flushPromises();
  expect(server.calls).toContainEqual({ method: "POST", url: "/v1/airlie/chat/threads", body: { kind: "plain" } });
  expect(r.currentRoute.value.fullPath).toBe("/chat/new-1");
});

test("a chat that could not be made shows why", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/v1/events") return new Promise<Response>(() => {});
    if (init?.method === "POST") return Response.json({ detail: "no VM is free" }, { status: 409 });
    return Response.json(url.endsWith("pins") ? { pins: [] } : url.endsWith("names") ? { repos: [] } : []);
  }));
  const { w, r } = await open();
  await button(w, "New chat").trigger("click");
  await flushPromises();
  expect(w.get('[role="alert"]').text()).toContain("no VM is free");
  expect(r.currentRoute.value.fullPath).toBe("/");
});

test("pinning and unpinning move a repository between its group and other repositories", async () => {
  panelServer({ repos: ["acme/api"], threads: [REPO("a", "acme/api", 10, "api chat")] });
  const { w } = await open();
  expect(w.find('section[aria-label="Other repositories"]').text()).toContain("api chat");

  await w.get('[role="combobox"]').setValue("acme/api");
  await button(w, "Pin").trigger("click");
  await flushPromises();
  expect(w.find('section[aria-label="Other repositories"]').exists()).toBe(false);
  expect(rowTitles(w, "acme/api")).toEqual(["api chat"]);

  await button(w, "Unpin acme/api").trigger("click");
  await flushPromises();
  expect(w.find('section[aria-label="Other repositories"]').text()).toContain("api chat");
});

test("chats of an unpinned repository stay listed under other repositories", async () => {
  panelServer({ pins: ["acme/api"], threads: [REPO("g", "gone/repo", 10, "orphan chat")] });
  const { w } = await open();
  const other = w.get('section[aria-label="Other repositories"]');
  expect(other.text()).toContain("gone/repo");
  expect(other.text()).toContain("orphan chat");
  expect(other.find('[aria-label="Unpin gone/repo"]').exists()).toBe(false);
});

test("with no pins and no chats it says pin a repository to start a chat in a VM", async () => {
  panelServer();
  const { w } = await open();
  expect(w.text()).toContain("Pin a repository to start a chat in a VM");
  expect(w.find('section[aria-label="Other repositories"]').exists()).toBe(false);
});

test("a failed load shows the error with retry", async () => {
  const server = panelServer({ threads: [thread("c1", { title: "back again" })], down: new Set(["threads"]) });
  const { w } = await open();
  expect(w.get('[role="alert"]').text()).toContain("threads are down");
  expect(w.text()).not.toContain("Loading chats");
  server.down.clear();
  await button(w, "Retry").trigger("click");
  await flushPromises();
  expect(w.find('[role="alert"]').exists()).toBe(false);
  expect(w.text()).toContain("back again");
});

test("while loading it shows no stale rows", async () => {
  setActivePinia(createPinia());
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
  const w = mount(ChatPanel, { props: { currentId: null }, global: { plugins: [router()] } });
  await flushPromises();
  expect(w.get('[role="status"]').text()).toBe("Loading chats");
  expect(w.text()).not.toContain("Pin a repository to start");
  expect(w.find('[aria-label="New chat"]').exists()).toBe(false);
});

test("the current chat is marked current", async () => {
  panelServer({ threads: [thread("c1", { title: "one" }), thread("c2", { title: "two" })] });
  const { w } = await open("c2");
  const current = w.findAll('[aria-current="page"]');
  expect(current).toHaveLength(1);
  expect(current[0].text()).toBe("two");
});

test("picking a chat opens it", async () => {
  panelServer({ threads: [thread("c1", { title: "one" })] });
  const { w, r } = await open();
  await w.findAll("button").find((b) => b.text() === "one")!.trigger("click");
  await flushPromises();
  expect(r.currentRoute.value.fullPath).toBe("/chat/c1");
  expect(w.emitted("pick")).toEqual([["c1"]]);
});

test("a chat hint refetches the list", async () => {
  const server = panelServer({ threads: [thread("c1", { title: "one" })] });
  const { w } = await open();
  server.threads.push(thread("c2", { title: "from another device" }));
  feed.opts?.onChange({ topic: "chat", key: null });
  await flushPromises();
  expect(w.text()).toContain("from another device");
});

test("the close button asks to close", async () => {
  panelServer();
  const { w } = await open();
  await button(w, "Close the panel").trigger("click");
  expect(w.emitted("close")).toHaveLength(1);
});
