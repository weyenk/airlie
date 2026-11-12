import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import ChatPanel from "./ChatPanel.vue";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
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
  confirmDialog = mount(ConfirmDialog, { attachTo: document.body });
  const w = mount(ChatPanel, { props: { currentId }, global: { plugins: [r] }, attachTo: document.body }); // a form submits only when it is in the document
  await flushPromises();
  return { w, r };
}

let confirmDialog: ReturnType<typeof mount> | undefined;
afterEach(() => {
  confirmDialog?.unmount(); // one dialog for the app: a stale one would answer the next question
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

const REPO = (id: string, repo: string, updated: number, title = "") =>
  thread(id, { kind: "repository", repo, branch: `airlie/chat-${id}`, title, updated });

const button = (w: Awaited<ReturnType<typeof open>>["w"], name: string) => w.findAll("button").find((b) => b.attributes("aria-label") === name || b.text() === name)!;
const rowTitles = (w: Awaited<ReturnType<typeof open>>["w"], group: string) =>
  w.get(`section[aria-label="${group}"]`).findAll("button:not([aria-label]) > span:first-child > span:first-child").map((t) => t.text());

test("each pinned repository lists its chats, newest first", async () => {
  panelServer({
    pins: ["acme/api", "acme/web"],
    threads: [REPO("a", "acme/api", 10, "older"), REPO("b", "acme/api", 20, "newer"), REPO("c", "acme/web", 5, "web chat")],
  });
  const { w } = await open();
  const h3s = w.findAll("h3").map((h) => h.text());
  expect(h3s.indexOf("Chats")).toBeLessThan(h3s.indexOf("acme/api"));
  expect(h3s.indexOf("Chats")).toBeLessThan(h3s.indexOf("acme/web"));
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
    if (url.startsWith("/v1/airlie/chat/repositories")) {
      return Response.json({ sources: [{ name: "github_app", title: "GitHub", linked: false, login: null, repositories: [], error: null, listed_at: null }], extras: [], extra_candidates: [] });
    }
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
  expect(w.text()).toContain("Connect GitHub");
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
  expect(current[0].text()).toContain("two");
});

test("picking a chat opens it", async () => {
  panelServer({ threads: [thread("c1", { title: "one" })] });
  const { w, r } = await open();
  await w.findAll("button").find((b) => b.text().startsWith("one"))!.trigger("click");
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

async function deleteChat(w: Awaited<ReturnType<typeof open>>["w"], title: string, confirm = true) {
  const row = w.findAll('[data-test="chat-row"]').find((r) => r.text().includes(title))!;
  await row.get('[aria-label="Chat actions"]').trigger("keydown", { key: "Enter" });
  await flushPromises();
  [...document.body.querySelectorAll<HTMLElement>('[role="menuitem"]')].find((i) => i.textContent?.trim() === "Delete")!.click();
  await flushPromises();
  document.body.querySelector<HTMLElement>(confirm ? "[data-confirm-ok]" : "[data-confirm-cancel]")!.click();
  await flushPromises();
}

test("deleting a chat asks in the page, deletes it and refetches the list", async () => {
  const server = panelServer({ threads: [thread("c1", { title: "one", updated: 2 }), thread("c2", { title: "two", updated: 1 })] });
  const { w } = await open();
  await deleteChat(w, "two");
  expect(server.calls).toContainEqual({ method: "DELETE", url: "/v1/airlie/chat/threads/c2", body: undefined });
  expect(w.text()).not.toContain("two");
  expect(w.text()).toContain("one");
});

test("cancelling the delete question keeps the chat", async () => {
  const server = panelServer({ threads: [thread("c1", { title: "one" })] });
  const { w } = await open();
  await deleteChat(w, "one", false);
  expect(server.calls.some((c) => c.method === "DELETE")).toBe(false);
  expect(w.text()).toContain("one");
});

test("deleting the open chat opens the next chat", async () => {
  panelServer({ threads: [thread("c1", { title: "one", updated: 2 }), thread("c2", { title: "two", updated: 1 })] });
  const { w, r } = await open("c1");
  await r.push("/chat/c1");
  await deleteChat(w, "one");
  expect(r.currentRoute.value.fullPath).toBe("/chat/c2");
});

test("deleting the only chat that is open goes to the chat start", async () => {
  panelServer({ threads: [thread("c1", { title: "one" })] });
  const { w, r } = await open("c1");
  await r.push("/chat/c1");
  await deleteChat(w, "one");
  expect(r.currentRoute.value.fullPath).toBe("/");
});

test("deleting a chat whose turn is running shows the reason and keeps the row", async () => {
  panelServer({ threads: [thread("c1", { title: "one" })], running: new Set(["c1"]) });
  const { w, r } = await open("c1");
  await r.push("/chat/c1");
  await deleteChat(w, "one");
  expect(w.get('[role="alert"]').text()).toContain("a turn is running; stop it first");
  expect(w.text()).toContain("one");
  expect(r.currentRoute.value.fullPath).toBe("/chat/c1");
});

test("the close button asks to close", async () => {
  panelServer();
  const { w } = await open();
  await button(w, "Close the panel").trigger("click");
  expect(w.emitted("close")).toHaveLength(1);
});

test("an unlinked person sees connect github and their extras", async () => {
  panelServer({ repos: ["local/only"], pins: ["local/only"] });
  const { w } = await open();
  expect(w.text()).toContain("Connect GitHub");
  expect(w.find('section[aria-label="local/only"]').exists()).toBe(true);
});

test("connect shows the code and the link then the repositories once approved", async () => {
  vi.useFakeTimers();
  const server = panelServer({ link: { pollsBeforeLinked: 1, interval: 5 } });
  const { w } = await open();
  await w.findAll("button").find((b) => b.text() === "Connect GitHub")!.trigger("click");
  await flushPromises();
  expect(w.get('[data-test="link-code"]').text()).toBe("WXYZ-0000");
  expect(w.text()).toContain("https://github.com/login/device");
  await vi.advanceTimersByTimeAsync(5000);
  await flushPromises();
  await vi.advanceTimersByTimeAsync(5000);
  await flushPromises();
  expect(w.find('section[aria-label="weyenk/airlie"]').exists()).toBe(true);
  const polls = server.calls.filter((c) => c.method === "POST" && c.url.includes("/link/link-1"));
  expect(polls).toHaveLength(2);
  vi.useRealTimers();
});

test("a slow_down waits the new interval", async () => {
  vi.useFakeTimers();
  panelServer({ link: { pollsBeforeLinked: 3, interval: 5, slowAfter: 1, slowInterval: 10 } });
  const { w } = await open();
  await w.findAll("button").find((b) => b.text() === "Connect GitHub")!.trigger("click");
  await flushPromises();
  await vi.runAllTimersAsync();
  await flushPromises();
  expect(w.find('section[aria-label="weyenk/airlie"]').exists()).toBe(true);
  vi.useRealTimers();
});

test("a declined code says so and offers start again", async () => {
  vi.useFakeTimers();
  const server = panelServer({ link: { decline: true } });
  const { w } = await open();
  await w.findAll("button").find((b) => b.text() === "Connect GitHub")!.trigger("click");
  await flushPromises();
  await vi.advanceTimersByTimeAsync(5000);
  await flushPromises();
  expect(w.text()).toContain("you declined on GitHub");
  expect(w.text()).toContain("Start again");
  expect(server.link.decline).toBe(true);
  vi.useRealTimers();
});

test("an expired code offers start again", async () => {
  vi.useFakeTimers();
  panelServer({ link: { expire: true } });
  const { w } = await open();
  await w.findAll("button").find((b) => b.text() === "Connect GitHub")!.trigger("click");
  await flushPromises();
  await vi.advanceTimersByTimeAsync(5000);
  await flushPromises();
  expect(w.text()).toContain("the code expired: start again");
  expect(w.text()).toContain("Start again");
  vi.useRealTimers();
});

test("a link lost to a restart offers start again", async () => {
  vi.useFakeTimers();
  panelServer({ link: { gone: true } });
  const { w } = await open();
  await w.findAll("button").find((b) => b.text() === "Connect GitHub")!.trigger("click");
  await flushPromises();
  await vi.advanceTimersByTimeAsync(5000);
  await flushPromises();
  expect(w.text()).toContain("The code is gone: start again");
  expect(w.find('[role="alert"]').exists()).toBe(false);
  vi.useRealTimers();
});

test("coming back to the page polls at once", async () => {
  vi.useFakeTimers();
  const server = panelServer({ link: { pollsBeforeLinked: 2, interval: 60 } });
  const { w } = await open();
  await w.findAll("button").find((b) => b.text() === "Connect GitHub")!.trigger("click");
  await flushPromises();
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
  document.dispatchEvent(new Event("visibilitychange"));
  await vi.advanceTimersByTimeAsync(1000);
  const pollsBefore = server.calls.filter((c) => c.url.includes("/link/link-1")).length;
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
  document.dispatchEvent(new Event("visibilitychange"));
  await flushPromises();
  const pollsAfter = server.calls.filter((c) => c.url.includes("/link/link-1")).length;
  expect(pollsAfter).toBeGreaterThan(pollsBefore);
  vi.useRealTimers();
});

test("refresh repositories asks with refresh true", async () => {
  const server = panelServer({ linked: true, login: "weyenk", githubRepos: [{ repo: "weyenk/airlie", private: true, push: true, url: "" }] });
  const { w } = await open();
  await w.get('[data-test="refresh-repos"]').trigger("click");
  await flushPromises();
  expect(server.calls.some((c) => c.url === "/v1/airlie/chat/repositories?refresh=true")).toBe(true);
});

test("a source error shows its line and keeps the last list", async () => {
  panelServer({
    linked: true,
    login: "weyenk",
    githubRepos: [{ repo: "weyenk/airlie", private: true, push: true, url: "" }],
    sourceError: "GitHub is slow right now",
  });
  const { w } = await open();
  expect(w.text()).toContain("GitHub is slow right now");
  expect(w.find('section[aria-label="weyenk/airlie"]').exists()).toBe(true);
});

test("disconnect github returns to connect", async () => {
  const server = panelServer({
    linked: true,
    login: "weyenk",
    githubRepos: [{ repo: "weyenk/airlie", private: true, push: true, url: "" }],
  });
  const { w } = await open();
  await w.findAll("button").find((b) => b.text().includes("Disconnect GitHub"))!.trigger("click");
  await flushPromises();
  expect(server.calls.some((c) => c.method === "DELETE" && c.url.includes("/link"))).toBe(true);
  expect(w.text()).toContain("Connect GitHub");
});

test("github repositories cannot be unpinned but extras can", async () => {
  panelServer({
    linked: true,
    login: "weyenk",
    githubRepos: [{ repo: "weyenk/airlie", private: true, push: true, url: "" }],
    pins: ["local/only"],
    repos: ["local/only"],
    threads: [REPO("r1", "weyenk/airlie", 1, "gh"), REPO("r2", "local/only", 2, "local")],
  });
  const { w } = await open();
  expect(w.find('[aria-label="Unpin weyenk/airlie"]').exists()).toBe(false);
  await button(w, "Unpin local/only").trigger("click");
  await flushPromises();
  expect(w.text()).not.toContain("Unpin local/only");
});

test("the pin picker offers only local checkouts", async () => {
  panelServer({
    linked: true,
    login: "weyenk",
    githubRepos: [{ repo: "weyenk/airlie", private: true, push: true, url: "" }],
    repos: ["local/only"],
  });
  const { w } = await open();
  expect(w.text()).toContain("Pin a local checkout");
  await w.get('[role="combobox"]').setValue("l");
  expect(w.findAll('[role="option"]').map((o) => o.text())).toEqual([expect.stringContaining("local/only")]);
});

test("chats of a repository no longer listed stay under other repositories", async () => {
  panelServer({
    linked: true,
    login: "weyenk",
    githubRepos: [{ repo: "weyenk/airlie", private: true, push: true, url: "" }],
    threads: [REPO("g", "gone/repo", 10, "orphan chat")],
  });
  const { w } = await open();
  const other = w.get('section[aria-label="Other repositories"]');
  expect(other.text()).toContain("gone/repo");
  expect(other.text()).toContain("orphan chat");
});

test("plain chats appear before repository headings when both exist", async () => {
  panelServer({
    linked: true,
    login: "weyenk",
    githubRepos: [{ repo: "weyenk/airlie", private: true, push: true, url: "" }],
    pins: ["weyenk/airlie"],
    threads: [thread("p1", { title: "plain one" }), REPO("r1", "weyenk/airlie", 1, "repo chat")],
  });
  const { w } = await open();
  const html = w.html();
  expect(html.indexOf('aria-label="Plain chats"')).toBeLessThan(html.indexOf('aria-label="weyenk/airlie"'));
  expect(w.text()).toContain("plain one");
  expect(w.text()).toContain("repo chat");
});

test("plain chats are not nested under the github section", async () => {
  panelServer({
    linked: true,
    login: "weyenk",
    githubRepos: [{ repo: "weyenk/airlie", private: true, push: true, url: "" }],
    threads: [thread("p1", { title: "plain one" })],
  });
  const { w } = await open();
  const github = w.get('section[aria-label="GitHub"]');
  const plain = w.get('section[aria-label="Plain chats"]');
  expect(github.find('section[aria-label="Plain chats"]').exists()).toBe(false);
  expect(plain.element.closest('section[aria-label="GitHub"]')).toBeNull();
  expect(github.get("h3").text()).toBe("GitHub");
  expect(plain.classes()).toContain("border-t");
});

test("plain chats list shows at most six, newest first", async () => {
  const threads = Array.from({ length: 8 }, (_, i) =>
    thread(`c${i}`, { title: `chat ${i}`, updated: 100 - i, created: 100 - i }),
  );
  panelServer({ threads });
  const { w } = await open();
  expect(w.findAll('[data-test="chat-row"]')).toHaveLength(6);
  const titles = rowTitles(w, "Plain chats");
  expect(titles).toHaveLength(6);
  expect(titles).toEqual(["chat 0", "chat 1", "chat 2", "chat 3", "chat 4", "chat 5"]);
});
