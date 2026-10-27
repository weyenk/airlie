import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory } from "vue-router";
import TopBar from "./TopBar.vue";
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

describe("TopBar", () => {
  it("the main tab bar contains a Wiki link to /wiki in addition to the five existing tabs", async () => {
    fakeFetch(); // signed in, so the guard passes and the shell renders
    const pinia = createPinia();
    setActivePinia(pinia);
    const router = createAppRouter(createMemoryHistory("/"));
    await router.push("/wiki");
    const w = mount(TopBar, { global: { plugins: [pinia, router] }, attachTo: document.body });
    await flushPromises();

    const tabs = w.find("nav[data-test=tabs]");
    expect(tabs.exists()).toBe(true);
    const links = tabs.findAll("a");
    const byText = new Map(links.map((a) => [a.text(), a.attributes("href")]));
    // the five existing tabs are still there, in order
    expect(links.map((a) => a.text())).toEqual(["Chat", "Workflows", "Board", "Metrics", "Admin", "Wiki"]);
    // the new tab: visible text exactly "Wiki", href "/wiki"
    expect(byText.get("Wiki")).toBe("/wiki");
    expect(byText.get("Chat")).toBe("/");
    expect(byText.get("Workflows")).toBe("/workflows");
    expect(byText.get("Board")).toBe("/board");
    expect(byText.get("Metrics")).toBe("/metrics");
    expect(byText.get("Admin")).toBe("/admin");
    w.unmount();
  });
});
