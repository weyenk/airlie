import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory } from "vue-router";
import App from "@/App.vue";
import { createAppRouter } from "@/router";

function fakeFetch(signedIn = true) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/v1/events") return new Promise<Response>(() => {});
      if (url === "/v1/auth/me") {
        return signedIn ? Response.json({ name: "Noah Kruswicki", role: "admin", via: "cookie", update_panel: true }) : Response.json({ error: "not signed in" }, { status: 401 });
      }
      return new Response("{}", { status: 404 });
    }),
  );
}
afterEach(() => vi.unstubAllGlobals());

async function open(path: string) {
  fakeFetch();
  const pinia = createPinia();
  setActivePinia(pinia);
  const router = createAppRouter(createMemoryHistory("/"));
  await router.push(path);
  const w = mount(App, { global: { plugins: [pinia, router] }, attachTo: document.body });
  await flushPromises();
  return { w, router };
}

test("the shell has the six tabs (Update included when panel enabled), marks the current one, and shows the avatar", async () => {
  const { w } = await open("/board");
  const tabs = w.find("[data-test=tabs]");
  expect(tabs.findAll("a").map((a) => a.text())).toEqual(["Chat", "Board", "Workflows", "Metrics", "Admin", "Update"]);
  expect(tabs.find("a[aria-current=page]").text()).toBe("Board");
  const bar = w.find("nav[data-test=tabbar]");
  expect(bar.findAll("a").map((a) => a.text())).toEqual(["Chat", "Board", "Workflows", "Metrics", "Admin", "Update"]);
  expect(w.find("[data-test=avatar]").text()).toBe("NK");
  expect(w.text()).toContain("Board");
  w.unmount();
});

test("a story page keeps the Workflows tab marked", async () => {
  const { w } = await open("/workflows/AIR-9");
  expect(w.find("[data-test=tabs] a[aria-current=page]").text()).toBe("Workflows");
  w.unmount();
});

test("the sign-in screen is bare: no tabs, no top bar", async () => {
  fakeFetch(false); // nobody known: a known person is handed straight on, and sees the shell
  const pinia = createPinia();
  setActivePinia(pinia);
  const router = createAppRouter(createMemoryHistory("/"));
  await router.push("/signin");
  const w = mount(App, { global: { plugins: [pinia, router] } });
  await flushPromises();
  expect(w.find("[data-test=tabs]").exists()).toBe(false);
  expect(w.find("h1").text()).toBe("Sign in");
  w.unmount();
});

// ---- Nav gating integration: Update tab appears only when panel is on AND admin ----

test("no update_panel on nav — Admin sees Update tab, member does not", async () => {
  // Sub-scenario 1: admin with update_panel: true — Update appears.
  fakeFetch(true); // returns { role: "admin", update_panel: true }
  const pinia = createPinia();
  setActivePinia(pinia);
  const router = createAppRouter(createMemoryHistory("/"));
  await router.push("/");
  const w = mount(App, { global: { plugins: [pinia, router] }, attachTo: document.body });
  await flushPromises();
  const tabs = w.find("[data-test=tabs]");
  expect(tabs.findAll("a").map((a) => a.text())).toContain("Update");
  const bar = w.find("nav[data-test=tabbar]");
  expect(bar.findAll("a").map((a) => a.text())).toContain("Update");
  w.unmount();

  // Sub-scenario 2: member with update_panel: true — Update absent (nav gates on role).
  // Re-stub fetch for a member response (update_panel is present but role != admin).
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/v1/events") return new Promise<Response>(() => {});
      if (url === "/v1/auth/me") {
        return Response.json({ name: "Member", role: "member" as const, via: "session", update_panel: true });
      }
      return new Response("{}", { status: 404 });
    }),
  );
  const memberPinia = createPinia();
  setActivePinia(memberPinia);
  const memberRouter = createAppRouter(createMemoryHistory("/"));
  await memberRouter.push("/");
  const memberW = mount(App, { global: { plugins: [memberPinia, memberRouter] }, attachTo: document.body });
  await flushPromises();
  const memberTabs = memberW.find("[data-test=tabs]");
  expect(memberTabs.findAll("a").map((a) => a.text())).not.toContain("Update");
  const memberBar = memberW.find("nav[data-test=tabbar]");
  expect(memberBar.findAll("a").map((a) => a.text())).not.toContain("Update");
  memberW.unmount();

  // Sub-scenario 3: admin with update_panel: false — Update absent (panel off).
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/v1/events") return new Promise<Response>(() => {});
      if (url === "/v1/auth/me") {
        return Response.json({ name: "Admin", role: "admin" as const, via: "session", update_panel: false });
      }
      return new Response("{}", { status: 404 });
    }),
  );
  const panelOffPinia = createPinia();
  setActivePinia(panelOffPinia);
  const panelOffRouter = createAppRouter(createMemoryHistory("/"));
  await panelOffRouter.push("/");
  const panelOffW = mount(App, { global: { plugins: [panelOffPinia, panelOffRouter] }, attachTo: document.body });
  await flushPromises();
  const panelOffTabs = panelOffW.find("[data-test=tabs]");
  expect(panelOffTabs.findAll("a").map((a) => a.text())).not.toContain("Update");
  const panelOffBar = panelOffW.find("nav[data-test=tabbar]");
  expect(panelOffBar.findAll("a").map((a) => a.text())).not.toContain("Update");
  panelOffW.unmount();
});
