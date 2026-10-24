import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory } from "vue-router";
import { createAppRouter } from "./router";
import { request } from "./api";
import { useFeedStore } from "./stores/feed";
import { useSessionStore } from "./stores/session";

const ME = { name: "noah", role: "admin", via: "cookie" };

function fakeFetch(me: "signed-in" | "signed-out") {
  const f = vi.fn(async (url: string) => {
    if (url === "/v1/events") return new Promise<Response>(() => {}); // the feed never answers
    if (url === "/v1/auth/me" && me === "signed-in") return Response.json(ME);
    return new Response(JSON.stringify({ detail: "no" }), { status: 401 });
  });
  vi.stubGlobal("fetch", f);
  return f;
}

beforeEach(() => {
  setActivePinia(createPinia());
});
afterEach(() => {
  vi.unstubAllGlobals();
});

test("a signed-out visit goes to sign in and keeps the page asked for", async () => {
  fakeFetch("signed-out");
  const router = createAppRouter(createMemoryHistory("/next/"));
  await router.push("/board/AIR-17");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/signin?next=/board/AIR-17");
});

test("a signed-in visit loads me once and starts the feed", async () => {
  const f = fakeFetch("signed-in");
  const router = createAppRouter(createMemoryHistory("/next/"));
  await router.push("/board");
  await router.push("/metrics");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/metrics");
  expect(f.mock.calls.filter(([u]) => u === "/v1/auth/me")).toHaveLength(1);
  expect(useSessionStore().me).toEqual(ME);
  expect(f.mock.calls.filter(([u]) => u === "/v1/events")).toHaveLength(1);
});

test("the old start and job addresses redirect", async () => {
  fakeFetch("signed-in");
  const router = createAppRouter(createMemoryHistory("/next/"));
  await router.push("/start");
  expect(router.currentRoute.value.fullPath).toBe("/");
  await router.push("/jobs/r1");
  expect(router.currentRoute.value.fullPath).toBe("/workflows/runs/r1");
});

test("a run address is never read as a story called runs", async () => {
  fakeFetch("signed-in");
  const router = createAppRouter(createMemoryHistory("/next/"));
  await router.push("/workflows/runs/r1/node/job");
  expect(router.currentRoute.value.name).toBe("run");
  await router.push("/workflows/AIR-9/n1");
  expect(router.currentRoute.value.name).toBe("story");
  expect(router.currentRoute.value.meta.tab).toBe("workflows");
});

test("sign in and join are public and bare; unknown addresses are not found", async () => {
  fakeFetch("signed-out");
  const router = createAppRouter(createMemoryHistory("/next/"));
  await router.push("/signin");
  expect(router.currentRoute.value.meta).toMatchObject({ public: true, bare: true });
  await router.push("/join/abc");
  expect(router.currentRoute.value.fullPath).toBe("/join/abc");
  expect(useFeedStore().state).toBe("connecting");
  fakeFetch("signed-in");
  await router.push("/nope/at/all");
  expect(router.currentRoute.value.name).toBe("not-found");
});

test("a 401 from any call stops the feed", async () => {
  fakeFetch("signed-in");
  const router = createAppRouter(createMemoryHistory("/next/"));
  await router.push("/board");
  await flushPromises();
  const feed = useFeedStore();
  const stop = vi.spyOn(feed, "stop");
  feed.state = "reconnecting";
  await expect(request("/v1/board")).rejects.toMatchObject({ status: 401 });
  await flushPromises();
  expect(stop).toHaveBeenCalledTimes(1);
  expect(feed.state).not.toBe("reconnecting");
  expect(useSessionStore().me).toBeNull();
  expect(router.currentRoute.value.fullPath).toBe("/signin?next=/board");
});

test("signing out posts the logout and goes to sign in", async () => {
  const f = fakeFetch("signed-in");
  const router = createAppRouter(createMemoryHistory("/next/"));
  await router.push("/admin");
  await useSessionStore().signOut();
  await flushPromises();
  expect(f).toHaveBeenCalledWith("/v1/auth/logout", expect.objectContaining({ method: "POST" }));
  expect(router.currentRoute.value.fullPath).toBe("/signin?next=/admin");
});

test("the feed stops when a public page is visited after sign-in", async () => {
  fakeFetch("signed-in");
  const router = createAppRouter(createMemoryHistory("/next/"));
  await router.push("/board");
  await flushPromises();
  const feed = useFeedStore();
  feed.state = "live";
  const stop = vi.spyOn(feed, "stop");
  await router.push("/signin");
  expect(stop).toHaveBeenCalledTimes(1);
  expect(feed.state).toBe("connecting");
});

test("the feed answering 401 signs the session out", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/v1/auth/me") return Response.json(ME);
      return new Response("no", { status: 401 }); // the feed itself
    }),
  );
  const router = createAppRouter(createMemoryHistory("/next/"));
  await router.push("/board");
  await flushPromises();
  expect(useSessionStore().me).toBeNull();
  expect(router.currentRoute.value.fullPath).toBe("/signin?next=/board");
});
