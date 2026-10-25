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
        return signedIn ? Response.json({ name: "Noah Kruswicki", role: "admin", via: "cookie" }) : Response.json({ error: "not signed in" }, { status: 401 });
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
  const router = createAppRouter(createMemoryHistory("/next/"));
  await router.push(path);
  const w = mount(App, { global: { plugins: [pinia, router] }, attachTo: document.body });
  await flushPromises();
  return { w, router };
}

test("the shell has the five tabs, marks the current one, and shows the avatar", async () => {
  const { w } = await open("/board");
  const tabs = w.find("[data-test=tabs]");
  expect(tabs.findAll("a").map((a) => a.text())).toEqual(["Chat", "Workflows", "Board", "Metrics", "Admin"]);
  expect(tabs.find("a[aria-current=page]").text()).toBe("Board");
  const bar = w.find("nav[data-test=tabbar]");
  expect(bar.findAll("a").map((a) => a.text())).toEqual(["Chat", "Workflows", "Board", "Metrics", "Admin"]);
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
  const router = createAppRouter(createMemoryHistory("/next/"));
  await router.push("/signin");
  const w = mount(App, { global: { plugins: [pinia, router] } });
  await flushPromises();
  expect(w.find("[data-test=tabs]").exists()).toBe(false);
  expect(w.find("h1").text()).toBe("Sign in");
  w.unmount();
});
