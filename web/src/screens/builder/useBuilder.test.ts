import { flushPromises } from "@vue/test-utils";
import { ref } from "vue";
import { ApiError } from "@/api";
import { useBuilder } from "./useBuilder";

const api = vi.hoisted(() => ({ getWorkflowDef: vi.fn(), registryCatalog: vi.fn(), deleteDraft: vi.fn(), saveDraft: vi.fn() }));
vi.mock("@/api/workflow_defs", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/workflow_defs")>()), ...api }));

beforeEach(() => {
  api.registryCatalog.mockReset().mockRejectedValue(new Error("no catalog"));
  api.deleteDraft.mockReset().mockResolvedValue(undefined);
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
