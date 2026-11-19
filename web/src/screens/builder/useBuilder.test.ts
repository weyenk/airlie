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
  expect(node.kind).toBe("WORK");
  expect(node.spec).toMatchObject({ workflow: "layer" });
  expect(b.graph.value.nodes.map((n) => n.kind)).toEqual(["WORK"]);
  expect(b.selected.value).toBe(node.id);
});

test("a meeting is placed with the protocol's roles and inputs from the catalog", async () => {
  api.registryCatalog.mockResolvedValueOnce(CATALOG);
  const b = useBuilder(ref("new"));
  await flushPromises();
  b.place({ kind: "ROUND_ROBIN" });
  expect(b.graph.value.nodes[0].spec).toMatchObject({ role: ["product", "architect"], inputs: ["story", "layers"] });
});

test("an output rename follows into its readers, or is refused through a draft prompt file that names it", async () => {
  const b = useBuilder(ref("new"));
  await flushPromises();
  b.graph.value = fromWorkflow("t", {
    inputs: [],
    outputs: [],
    nodes: {
      survey: { kind: "WORK", outputs: { survey: "markdown" }, to: ["plan"] },
      plan: { kind: "WORK", prompt: "Read {survey}.", inputs: ["survey"] },
      brief: { kind: "WORK", prompt: "prompt:brief", inputs: ["plan"] },
      planner: { kind: "WORK", outputs: { plan: "text" }, to: ["brief"] },
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

test("loading the shipped story reads no other workflow: every box is self-contained, and the YAML is unchanged", async () => {
  api.getWorkflowDef.mockReset().mockImplementation(async (name: string) => shippedDetail(name));
  const b = useBuilder(ref("story"));
  await b.load("story");
  await flushPromises();
  expect(api.getWorkflowDef).toHaveBeenCalledWith("story"); // only the open workflow: a box names its member, it is not opened here
  expect((b.graph.value?.bundles ?? {}).layer).toBeUndefined(); // nothing is read in, so nothing fills the bundle list
  expect(b.dirty.value).toBe(false);
  expect(b.yamlText.value.startsWith("workflows:")).toBe(true); // comments are not kept, structure is
  expect(b.yamlText.value).toContain("story:");
});

test("a placed box's ports are its own: nothing is fetched for the workflow it names", async () => {
  api.getWorkflowDef.mockReset().mockImplementation(async (name: string) => shippedDetail(name));
  const b = useBuilder(ref("new"));
  await flushPromises();
  b.place({ bundle: "layer" });
  const id = b.graph.value.nodes[0].id;
  expect(outputsOfNode(b.graph.value, id)).toEqual([]); // ports follow what the box declares
  await flushPromises();
  expect(api.getWorkflowDef).not.toHaveBeenCalledWith("layer"); // naming a workflow opens nothing
  b.edit((g) => updateNode(g, id, { ...g.nodes[0].spec, outputs: { built: { format: "markdown" } } }));
  expect(outputsOfNode(b.graph.value, id)).toEqual(["built"]); // the box's own outputs are its ports
});

test("placing workflow boxes reads no other definitions and reports no error", async () => {
  api.getWorkflowDef.mockReset().mockRejectedValue(new Error("gone"));
  const b = useBuilder(ref("new"));
  await flushPromises();
  b.place({ bundle: "layer" });
  b.place({ bundle: "story" });
  await flushPromises();
  expect(api.getWorkflowDef).not.toHaveBeenCalled(); // nothing is opened, so nothing can fail
  expect(b.graph.value?.bundles ?? {}).toEqual({});
  expect(b.error.value).toBeNull();
});
