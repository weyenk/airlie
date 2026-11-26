import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory } from "vue-router";
import { createAppRouter } from "@/router";
import { useSessionStore } from "@/stores/session";
import TabBar from "./TabBar.vue";

// Production order matters here: main.ts mounts without awaiting router.isReady(), so on a cold
// load the bar first renders while session.me is still null and /v1/auth/me answers later. Below
// 640px this bar is the only nav, so a list frozen at mount drops Update for the whole page life
// (TopBar uses a computed and grows its entry; this one has to follow).

function coldLoad(who: () => Response) {
  let answer!: (r: Response) => void;
  const pending = new Promise<Response>((resolve) => {
    answer = resolve;
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/v1/auth/me") return pending;
      return new Promise<Response>(() => {}); // the feed and anything else: never answers
    }),
  );
  const pinia = createPinia();
  setActivePinia(pinia);
  const router = createAppRouter(createMemoryHistory("/board"));
  const w = mount(TabBar, { global: { plugins: [pinia, router] }, attachTo: document.body });
  return { w, session: useSessionStore(), resolveMe: () => answer(who()) };
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

test("the Update tab is missing while nobody is known, then appears when the session resolves", async () => {
  const { w, session, resolveMe } = coldLoad(() => Response.json({ name: "Noah Kruswicki", role: "admin", via: "cookie", update_panel: true }));
  await flushPromises();

  expect(session.me).toBeNull();
  expect(w.findAll("a").map((a) => a.text())).toEqual(["Chat", "Board", "Workflows", "Metrics", "Admin"]);

  resolveMe();
  await flushPromises();

  expect(session.me?.role).toBe("admin");
  expect(w.findAll("a").map((a) => a.text())).toEqual(["Chat", "Board", "Workflows", "Metrics", "Admin", "Update"]);
  w.unmount();
});

test("the bar stays without Update when the resolved person is a member", async () => {
  const { w, resolveMe } = coldLoad(() => Response.json({ name: "Jess", role: "member", via: "cookie", update_panel: true }));
  await flushPromises();
  resolveMe();
  await flushPromises();
  expect(w.findAll("a").map((a) => a.text())).not.toContain("Update");
  w.unmount();
});
