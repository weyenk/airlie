// The worker is a classic script; run it here against a fake `self`, cache store and fetch, then fire its events.

import { expect, test, vi } from "vitest";
import SRC from "../public/sw.js?raw";
const ORIGIN = "https://airlie.test";

type Handler = (event: unknown) => void;

function fakeCache(initial: Record<string, unknown> = {}) {
  const store = new Map<string, unknown>(Object.entries(initial));
  const key = (r: unknown) => (typeof r === "string" ? r : new URL((r as Request).url).pathname);
  return {
    store,
    match: vi.fn(async (r: unknown) => store.get(key(r))),
    put: vi.fn(async (r: unknown, v: unknown) => void store.set(key(r), v)),
    addAll: vi.fn(async (paths: string[]) => paths.forEach((p) => store.set(p, `precached ${p}`))),
  };
}

function loadWorker(opts: { cache?: ReturnType<typeof fakeCache>; names?: string[]; fetch?: ReturnType<typeof vi.fn>; windows?: unknown[] } = {}) {
  const cache = opts.cache ?? fakeCache();
  const windows = opts.windows ?? [];
  const handlers: Record<string, Handler> = {};
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type: string, h: Handler) => (handlers[type] = h),
    skipWaiting: vi.fn(),
    clients: { claim: vi.fn(async () => undefined), matchAll: vi.fn(async () => windows), openWindow: vi.fn(async () => undefined) },
    registration: { showNotification: vi.fn(async () => undefined) },
  };
  const caches = {
    open: vi.fn(async () => cache),
    keys: vi.fn(async () => opts.names ?? []),
    delete: vi.fn(async () => true),
  };
  const fetchFn = opts.fetch ?? vi.fn(async () => ({ ok: true, clone: () => "copy" }));
  new Function("self", "caches", "fetch", SRC)(self, caches, fetchFn);
  return {
    self,
    caches,
    cache,
    fetch: fetchFn,
    dispatch(type: string, event: unknown) {
      handlers[type](event);
    },
  };
}

function get(path: string, headers: Record<string, string> = {}, mode = "cors", base = ORIGIN, method = "GET") {
  return { url: base + path, method, mode, headers: new Headers(headers) };
}

function fetchEvent(request: unknown) {
  return { request, respondWith: vi.fn() };
}

function waitEvent() {
  const promises: Promise<unknown>[] = [];
  return { waitUntil: (p: Promise<unknown>) => promises.push(p), done: () => Promise.all(promises) };
}

test("api and event streams pass through", () => {
  const w = loadWorker();
  const passing = [
    get("/v1/board"),
    get("/v1/events", { accept: "text/event-stream" }),
    get("/x", { accept: "text/event-stream" }),
    get("/assets/app-1a2b.js", {}, "cors", "https://elsewhere.test"),
    get("/assets/app-1a2b.js", {}, "cors", ORIGIN, "POST"),
  ];
  for (const req of passing) {
    const e = fetchEvent(req);
    w.dispatch("fetch", e);
    expect(e.respondWith).not.toHaveBeenCalled();
  }
});

test("hashed assets are served cache-first", async () => {
  const hit = { ok: true, body: "cached" };
  const w = loadWorker({ cache: fakeCache({ "/assets/app-1a2b.js": hit }) });
  const e = fetchEvent(get("/assets/app-1a2b.js"));
  w.dispatch("fetch", e);
  expect(e.respondWith).toHaveBeenCalledTimes(1);
  expect(await e.respondWith.mock.calls[0][0]).toBe(hit);
  expect(w.fetch).not.toHaveBeenCalled();
});

test("a hashed asset not yet cached is fetched and stored", async () => {
  const w = loadWorker();
  const e = fetchEvent(get("/assets/new-9z.js"));
  w.dispatch("fetch", e);
  await e.respondWith.mock.calls[0][0];
  expect(w.fetch).toHaveBeenCalledTimes(1);
  expect(w.cache.put).toHaveBeenCalledTimes(1);
});

test("a navigation falls back to the cached shell, then to offline.html", async () => {
  const down = vi.fn(async () => {
    throw new TypeError("offline");
  });
  const withShell = loadWorker({ cache: fakeCache({ "/": "shell", "/offline.html": "offline page" }), fetch: down });
  const a = fetchEvent(get("/board", {}, "navigate"));
  withShell.dispatch("fetch", a);
  expect(await a.respondWith.mock.calls[0][0]).toBe("shell");

  const bare = loadWorker({ cache: fakeCache({ "/offline.html": "offline page" }), fetch: down });
  const b = fetchEvent(get("/board", {}, "navigate"));
  bare.dispatch("fetch", b);
  expect(await b.respondWith.mock.calls[0][0]).toBe("offline page");
});

test("a navigation goes to the network first", async () => {
  const live = { ok: true, body: "live" };
  const w = loadWorker({ cache: fakeCache({ "/": "shell" }), fetch: vi.fn(async () => live) });
  const e = fetchEvent(get("/board", {}, "navigate"));
  w.dispatch("fetch", e);
  expect(await e.respondWith.mock.calls[0][0]).toBe(live);
});

test("new worker waits and skips on message", async () => {
  const w = loadWorker();
  const install = waitEvent();
  w.dispatch("install", install);
  await install.done();
  expect(w.cache.addAll).toHaveBeenCalledTimes(1);
  expect(w.cache.addAll.mock.calls[0][0]).toEqual(expect.arrayContaining(["/", "/offline.html", "/manifest.webmanifest"]));
  expect(w.self.skipWaiting).not.toHaveBeenCalled();
  w.dispatch("message", { data: { type: "other" } });
  expect(w.self.skipWaiting).not.toHaveBeenCalled();
  w.dispatch("message", { data: { type: "SKIP_WAITING" } });
  expect(w.self.skipWaiting).toHaveBeenCalledTimes(1);
});

test("activate drops old airlie-shell caches", async () => {
  const w = loadWorker({ names: ["airlie-shell-old", "other"] });
  const activate = waitEvent();
  w.dispatch("activate", activate);
  await activate.done();
  expect(w.caches.delete).toHaveBeenCalledTimes(1);
  expect(w.caches.delete).toHaveBeenCalledWith("airlie-shell-old");
  expect(w.self.clients.claim).toHaveBeenCalledTimes(1);
});

function pushEvent(json: () => unknown) {
  return { data: { json }, ...waitEvent() };
}

test("a push shows its title, body and page", async () => {
  const w = loadWorker();
  const e = pushEvent(() => ({ title: "AIR-9 is blocked", url: "/board/AIR-9", body: "x" }));
  w.dispatch("push", e);
  await e.done();
  expect(w.self.registration.showNotification).toHaveBeenCalledWith("AIR-9 is blocked", {
    body: "x",
    data: { url: "/board/AIR-9" },
    icon: "/icon-192.png",
    badge: "/icon-192.png",
  });
});

test("a push with no or bad data still shows airlie", async () => {
  const w = loadWorker();
  const bad = pushEvent(() => {
    throw new SyntaxError("not json");
  });
  w.dispatch("push", bad);
  const none = { data: null, ...waitEvent() };
  w.dispatch("push", none);
  await Promise.all([bad.done(), none.done()]);
  expect(w.self.registration.showNotification).toHaveBeenCalledTimes(2);
  for (const call of w.self.registration.showNotification.mock.calls as unknown[][]) expect(call[0]).toBe("airlie");
});

test("tapping a notification focuses an open window and sends it to the page", async () => {
  const win = { focus: vi.fn(async () => undefined), navigate: vi.fn(async () => undefined) };
  const w = loadWorker({ windows: [win] });
  const e = { notification: { close: vi.fn(), data: { url: "/board/AIR-9" } }, ...waitEvent() };
  w.dispatch("notificationclick", e);
  await e.done();
  expect(e.notification.close).toHaveBeenCalled();
  expect(win.focus).toHaveBeenCalled();
  expect(win.navigate).toHaveBeenCalledWith("/board/AIR-9");
  expect(w.self.clients.openWindow).not.toHaveBeenCalled();
});

test("tapping a notification opens a window when none is open", async () => {
  const w = loadWorker();
  const e = { notification: { close: vi.fn(), data: { url: "/board/AIR-9" } }, ...waitEvent() };
  w.dispatch("notificationclick", e);
  await e.done();
  expect(w.self.clients.openWindow).toHaveBeenCalledWith("/board/AIR-9");
});
