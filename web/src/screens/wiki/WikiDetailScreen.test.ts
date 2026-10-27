import { flushPromises, mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import WikiDetailScreen from "./WikiDetailScreen.vue";

// The wiki module is the layer-01 data source; the screen only reads it. Stub it so the
// component test is deterministic and can also exercise the not-found path.
const wiki = vi.hoisted(() => ({ getPage: vi.fn() }));
vi.mock("@/wiki", () => wiki);

// The real page's H1, exactly as docs/wiki/multi-agent-architecture-on-mlx/01_core_compute_and_memory.md.
const H1 = "1. Core Computation & Memory Mechanics for Concurrent Agent Execution";
// A body with a "##" subheading, a fenced block, and a link — the elements the renderer must produce.
const BODY = `# ${H1}\n\n## Overview\n\nSome prose.\n\n\`\`\`python\nx = 1\n\`\`\`\n\nSee [the docs](https://example.com/docs).`;

async function open(path: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/wiki/:folder/:page", name: "wiki-page", component: WikiDetailScreen }],
  });
  await router.push(path);
  const w = mount(WikiDetailScreen, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return { w, router };
}

beforeEach(() => {
  wiki.getPage.mockReset();
});
afterEach(() => {
  document.body.innerHTML = "";
});

describe("WikiDetailScreen", () => {
  test("renders an existing page's body as formatted elements, not raw markdown", async () => {
    wiki.getPage.mockReturnValue({ folder: "multi-agent-architecture-on-mlx", page: "01_core_compute_and_memory", title: "Core compute and memory", body: BODY });
    const { w } = await open("/wiki/multi-agent-architecture-on-mlx/01_core_compute_and_memory");
    const text = w.text();
    // The "##" subheading becomes an <h2>, the fence becomes a <pre>, the link becomes an <a>.
    expect(w.find("h2").exists()).toBe(true);
    expect(w.find("h2").text()).toBe("Overview");
    expect(w.find("pre").exists()).toBe(true);
    expect(w.find("a").exists()).toBe(true);
    // The raw markdown markers are not present as visible text: SafeMarkdown ran, not a raw dump.
    expect(text).not.toContain("## ");
    expect(text).not.toContain("```");
  });

  test("shows the page H1 as the document title and is read-only (no edit control)", async () => {
    wiki.getPage.mockReturnValue({ folder: "multi-agent-architecture-on-mlx", page: "01_core_compute_and_memory", title: "Core compute and memory", body: BODY });
    const { w } = await open("/wiki/multi-agent-architecture-on-mlx/01_core_compute_and_memory");
    // The extracted H1 is the document title, and the body renders.
    expect(w.find("h1").exists()).toBe(true);
    expect(w.find("h1").text()).toBe(H1);
    expect(w.find("pre").exists()).toBe(true);
    // Read-only: no edit affordance is ever emitted.
    expect(w.find("button").exists()).toBe(false);
    expect(w.find("input").exists()).toBe(false);
    expect(w.find("textarea").exists()).toBe(false);
    expect(w.find("[contenteditable]").exists()).toBe(false);
  });

  test("shows 'Wiki page not found' and no document body for a missing page", async () => {
    wiki.getPage.mockReturnValue(null);
    const { w } = await open("/wiki/multi-agent-architecture-on-mlx/99_does_not_exist");
    expect(w.text()).toContain("Wiki page not found");
    // No document body renders.
    expect(w.find("h1").exists()).toBe(false);
    expect(w.find("pre").exists()).toBe(false);
    expect(w.find("p").exists()).toBe(false);
  });
});
