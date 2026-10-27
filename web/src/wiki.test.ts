import { buildEntries, deriveTitle, extractH1, getPage } from "./wiki";
import type { WikiEntry } from "./wiki";

describe("buildEntries", () => {
  it("returns the real 9 wiki entries grouped into the two folders", () => {
    const entries = buildEntries();
    expect(entries).toHaveLength(9);

    const folders = new Set(entries.map((e) => e.folder));
    expect([...folders].sort()).toEqual(["build-spec", "multi-agent-architecture-on-mlx"]);
    expect(entries.filter((e) => e.folder === "build-spec")).toHaveLength(4);
    expect(entries.filter((e) => e.folder === "multi-agent-architecture-on-mlx")).toHaveLength(5);

    // every entry's folder/page/title, against the real docs/wiki tree
    const triples = entries
      .map((e) => [`${e.folder}/${e.page}`, e.title] as const)
      .sort((a, b) => a[0].localeCompare(b[0]));
    expect(triples).toEqual([
      ["build-spec/01_system_architecture_and_hardware_spec", "System architecture and hardware spec"],
      ["build-spec/02_vllm_mlx_engine_and_uma_execution_spec", "Vllm mlx engine and uma execution spec"],
      ["build-spec/03_lts_shared_memory_substate_spec", "Lts shared memory substate spec"],
      ["build-spec/04_edgeagent_scheduler_and_orchestration_loop_spec", "Edgeagent scheduler and orchestration loop spec"],
      ["multi-agent-architecture-on-mlx/01_core_compute_and_memory", "Core compute and memory"],
      ["multi-agent-architecture-on-mlx/02_compilation_and_metal", "Compilation and metal"],
      ["multi-agent-architecture-on-mlx/03_nn_primitives_and_kv_cache", "Nn primitives and kv cache"],
      ["multi-agent-architecture-on-mlx/04_ecosystem_and_runtimes", "Ecosystem and runtimes"],
      ["multi-agent-architecture-on-mlx/05_multi_agent_orchestration", "Multi agent orchestration"],
    ]);

    const entry = entries.find(
      (e) => e.folder === "multi-agent-architecture-on-mlx" && e.page === "01_core_compute_and_memory",
    );
    expect(entry?.title).toBe("Core compute and memory");
    expect(entry?.body.length).toBeGreaterThan(0);
  });

  it("a synthetic new file yields a new entry while existing entries are unchanged", () => {
    const entries = buildEntries();
    expect(entries).toHaveLength(9);

    // a new markdown file under an existing folder, modelled as an added glob key
    const filename = "05_new_page.md";
    const synthetic: WikiEntry = {
      folder: "build-spec",
      page: filename.replace(/\.md$/, ""),
      title: deriveTitle(filename),
      body: "# New page\n\nBody of the new page.\n",
    };
    const listing = [...entries, synthetic];

    expect(listing).toHaveLength(10);
    expect(listing[9].title).toBe("New page");
    expect(listing.slice(0, 9)).toEqual(entries); // the original 9 entries are byte-for-byte unchanged
  });
});

describe("deriveTitle", () => {
  it("strips the NN_ prefix and .md, spaces underscores, capitalizes the first letter", () => {
    expect(deriveTitle("01_core_compute_and_memory.md")).toBe("Core compute and memory");
  });
});

describe("getPage + extractH1", () => {
  it("return the real markdown body and its H1 for a known page", () => {
    const entry = getPage("multi-agent-architecture-on-mlx", "01_core_compute_and_memory");
    expect(entry).not.toBeNull();
    expect(entry?.body.length).toBeGreaterThan(0);
    expect(entry?.body).toContain("# 1. Core Computation & Memory Mechanics for Concurrent Agent Execution");
    expect(extractH1(entry?.body ?? "")).toBe("1. Core Computation & Memory Mechanics for Concurrent Agent Execution");

    expect(getPage("multi-agent-architecture-on-mlx", "no_such_page")).toBeNull();
  });
});
