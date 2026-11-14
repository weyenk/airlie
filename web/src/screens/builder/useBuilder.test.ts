import { flushPromises } from "@vue/test-utils";
import { ref } from "vue";
import { ApiError } from "@/api";
import { CATALOG } from "@/test-utils/catalog";
import { LAYER_YAML, STORY_YAML } from "@/test-utils/workflow-yaml";
import { fromWorkflow, outputsOfNode, updateNode } from "./graph";
import { useBuilder } from "./useBuilder";

const api = vi.hoisted(() => ({ getWorkflowDef: vi.fn(), registryCatalog: vi.fn(), deleteDraft: vi.fn(), saveDraft: vi.fn() }));
vi.mock("@/api/workflow_defs", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflow_defs")>()), ...api }));

beforeEach(() => {
  api.registryCatalog.mockReset().mockRejectedValue(new Error("no catalog"));
  api.deleteDraft.mockReset().mockResolvedValue(undefined);
  api.getWorkflowDef.mockReset().mockRejectedValue(new Error("no such workflow"));
});

test("placing a bundle item adds a bundle node that runs it, with no each box", async () => {
  const b = useBuilder(ref("new"));
  await flushPromises();
  b.place({ bundle: "layer" });
  const [node] = b.graph.value.nodes;
  expect(node.type).toBe("bundle");
  expect(node.spec).toMatchObject({ workflow: "layer" });
  expect(b.graph.value.nodes.map((n) => n.type)).toEqual(["bundle"]);
  expect(b.selected.value).toBe(node.id);
});

test("a meeting is placed with the protocol's roles and inputs from the catalog", async () => {
  api.registryCatalog.mockResolvedValueOnce(CATALOG);
  const b = useBuilder(ref("new"));
  await flushPromises();
  b.place({ type: "meeting" });
  expect(b.graph.value.nodes[0].spec).toMatchObject({ role: ["product", "architect"], inputs: ["story", "layers"] });
});

test("an output rename follows into its readers, or is refused through a draft prompt file that names it", async () => {
  const b = useBuilder(ref("new"));
  await flushPromises();
  b.graph.value = fromWorkflow("t", {
    inputs: [],
    outputs: [],
    nodes: {
      survey: { type: "ask", outputs: { survey: "markdown" }, to: ["plan"] },
      plan: { type: "ask", prompt: "Read {survey}.", inputs: ["survey"] },
      brief: { type: "ask", prompt: "prompt:brief", inputs: ["plan"] },
      planner: { type: "ask", outputs: { plan: "text" }, to: ["brief"] },
    },
  });
  expect(b.renameOutput("survey", "survey", "findings")).toBe(true);
  expect(b.graph.value.nodes.find((n) => n.id === "plan")!.spec).toMatchObject({ prompt: "Read {findings}.", inputs: ["findings"] });
  b.prompts.value = { brief: "---\nkind: rule\nroles: [coder]\nfields: [plan]\nwhy: w\n---\n{plan}" };
  expect(b.renameOutput("planner", "plan", "steps")).toBe(false);
  expect(b.error.value).toBe("plan cannot be renamed: brief read it through a prompt file");
  b.prompts.value = { brief: "---\nkind: rule\nroles: [coder]\nfields: []\nwhy: w\n---\nGo." };
  expect(b.renameOutput("planner", "plan", "steps")).toBe(true);
});

test("removing a draft deletes it by name", async () => {
  const b = useBuilder(ref("fix"));
  await flushPromises();
  await expect(b.remove("fix")).resolves.toBe(true);
  expect(api.deleteDraft).toHaveBeenCalledWith("fix");
});

test("a 409 delete yields the workflows that use the draft, and says so as the error", async () => {
  api.deleteDraft.mockRejectedValueOnce(new ApiError(409, JSON.stringify({ used_by: ["story"] })));
  const b = useBuilder(ref("fix"));
  await flushPromises();
  b.error.value = "an older error";
  await expect(b.remove("fix")).resolves.toEqual({ refused: ["story"] });
  expect(b.error.value).toBe("fix is used by story: take it out of those first");
});

test("any other delete failure is shown as the error and answers false", async () => {
  api.deleteDraft.mockRejectedValueOnce(new Error("gone already"));
  const b = useBuilder(ref("fix"));
  await flushPromises();
  await expect(b.remove("fix")).resolves.toBe(false);
  expect(String(b.error.value)).toContain("gone already");
});

test("a renaming save the server refuses with a 409 says which workflows use the draft", async () => {
  api.saveDraft.mockRejectedValueOnce(new ApiError(409, JSON.stringify({ used_by: ["story", "layer"] })));
  const b = useBuilder(ref("new"));
  await flushPromises();
  await expect(b.save("fix", "bug")).resolves.toEqual({ refused: "fix is used by story, layer: take it out of those first" });
});

const shippedDetail = (name: string) => ({
  name,
  type: name === "story" ? "story" : null,
  source: "file" as const,
  yaml: name === "story" ? STORY_YAML : LAYER_YAML,
  layout: {},
  problems: [],
  prompts: {},
  pr: null,
});

test("loading the shipped story reads the ports of the bundle its build_layers box runs; the YAML is unchanged", async () => {
  api.getWorkflowDef.mockReset().mockImplementation(async (name: string) => shippedDetail(name));
  const b = useBuilder(ref("story"));
  await b.load("story");
  await flushPromises();
  expect(api.getWorkflowDef).toHaveBeenCalledWith("layer");
  expect(b.graph.value.bundles.layer).toEqual({ inputs: ["card", "repo", "story", "layers", "base", "gate", "agreement"], outputs: ["drafted", "built.{item}", "satisfied.{item}"] });
  expect(b.dirty.value).toBe(false);
  expect(b.yamlText.value).not.toContain("bundles");
});

test("a bundle box placed from the palette gets its bundle's ports: it offers the bundle's outputs", async () => {
  api.getWorkflowDef.mockReset().mockImplementation(async (name: string) => shippedDetail(name));
  const b = useBuilder(ref("new"));
  await flushPromises();
  b.place({ bundle: "layer" });
  const id = b.graph.value.nodes[0].id;
  expect(outputsOfNode(b.graph.value, id)).toEqual([]); // not read yet
  await flushPromises();
  expect(api.getWorkflowDef).toHaveBeenCalledWith("layer");
  expect(outputsOfNode(b.graph.value, id)).toEqual(["drafted", "built", "satisfied"]);
  // a box whose workflow changes to one not read yet asks for it
  b.edit((g) => updateNode(g, id, { ...g.nodes[0].spec, workflow: "story" }));
  await flushPromises();
  expect(api.getWorkflowDef).toHaveBeenCalledWith("story");
  expect(b.graph.value.bundles.story).toEqual({ inputs: ["card", "repo"], outputs: [] });
});

test("a bundle that cannot be read leaves its box without ports, and is not asked for again until the next load", async () => {
  api.getWorkflowDef.mockReset().mockRejectedValue(new Error("gone"));
  const b = useBuilder(ref("new"));
  await flushPromises();
  b.place({ bundle: "layer" });
  await flushPromises();
  b.place({ bundle: "layer" });
  await flushPromises();
  expect(api.getWorkflowDef).toHaveBeenCalledTimes(1);
  expect(b.graph.value.bundles).toEqual({});
  expect(b.error.value).toBeNull();
});
