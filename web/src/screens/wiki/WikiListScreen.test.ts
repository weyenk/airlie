import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import WikiListScreen from "./WikiListScreen.vue";
import type { WikiEntry } from "@/wiki";

// the 9 real docs/wiki pages, exactly as buildEntries() derives them (folder/page/title)
const ENTRIES: WikiEntry[] = [
  { folder: "build-spec", page: "01_system_architecture_and_hardware_spec", title: "System architecture and hardware spec", body: "" },
  { folder: "build-spec", page: "02_vllm_mlx_engine_and_uma_execution_spec", title: "Vllm mlx engine and uma execution spec", body: "" },
  { folder: "build-spec", page: "03_lts_shared_memory_substate_spec", title: "Lts shared memory substate spec", body: "" },
  { folder: "build-spec", page: "04_edgeagent_scheduler_and_orchestration_loop_spec", title: "Edgeagent scheduler and orchestration loop spec", body: "" },
  { folder: "multi-agent-architecture-on-mlx", page: "01_core_compute_and_memory", title: "Core compute and memory", body: "" },
  { folder: "multi-agent-architecture-on-mlx", page: "02_compilation_and_metal", title: "Compilation and metal", body: "" },
  { folder: "multi-agent-architecture-on-mlx", page: "03_nn_primitives_and_kv_cache", title: "Nn primitives and kv cache", body: "" },
  { folder: "multi-agent-architecture-on-mlx", page: "04_ecosystem_and_runtimes", title: "Ecosystem and runtimes", body: "" },
  { folder: "multi-agent-architecture-on-mlx", page: "05_multi_agent_orchestration", title: "Multi agent orchestration", body: "" },
];

const wiki = vi.hoisted(() => ({ buildEntries: vi.fn() }));
vi.mock("@/wiki", () => wiki);

async function open() {
  setActivePinia(createPinia());
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/wiki", name: "wiki", component: WikiListScreen },
      { path: "/wiki/:folder/:page", name: "wiki-page", component: { template: "<i/>" } },
    ],
  });
  await router.push("/wiki");
  const w = mount(WikiListScreen, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return { w, router };
}

beforeEach(() => {
  wiki.buildEntries.mockReset().mockReturnValue(structuredClone(ENTRIES));
});
afterEach(() => {
  document.body.innerHTML = "";
});

describe("WikiListScreen", () => {
  it("renders 9 entries grouped under the two folder headings with derived titles", async () => {
    const { w } = await open();

    // exactly two sections, headed by their folder names
    const headings = w.findAll("h2").map((h) => h.text());
    expect(headings).toEqual(["build-spec", "multi-agent-architecture-on-mlx"]);

    // 9 list items total (4 + 5), one per page
    const items = w.findAll("li");
    expect(items).toHaveLength(9);

    // the 01_core_compute_and_memory entry: derived title, link to its detail route
    const core = items.find((li) => li.text() === "Core compute and memory");
    expect(core).toBeDefined();
    const link = core!.find("a");
    expect(link.attributes("href")).toBe("/wiki/multi-agent-architecture-on-mlx/01_core_compute_and_memory");

    // every entry's visible label is its derived title
    expect(items.map((li) => li.text())).toEqual([
      "System architecture and hardware spec",
      "Vllm mlx engine and uma execution spec",
      "Lts shared memory substate spec",
      "Edgeagent scheduler and orchestration loop spec",
      "Core compute and memory",
      "Compilation and metal",
      "Nn primitives and kv cache",
      "Ecosystem and runtimes",
      "Multi agent orchestration",
    ]);

    // the build-spec section holds its 4 entries
    const buildSpec = w.findAll("section")[0];
    expect(buildSpec.findAll("li")).toHaveLength(4);
    const mlx = w.findAll("section")[1];
    expect(mlx.findAll("li")).toHaveLength(5);
  });
});
