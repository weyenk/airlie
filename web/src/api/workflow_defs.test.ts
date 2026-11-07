import { callOf, stubJson } from "@/test-utils/fetch-stub";
import { deleteDraft, getWorkflowDef, listWorkflowDefs, publishWorkflow, registryCatalog, saveDraft, validateWorkflow } from "./workflow_defs";

afterEach(() => vi.unstubAllGlobals());

test("listWorkflowDefs unwraps the list", async () => {
  const fetchMock = stubJson({ workflows: [{ name: "story" }] });
  await expect(listWorkflowDefs()).resolves.toEqual([{ name: "story" }]);
  expect(callOf(fetchMock).url).toBe("/v1/workflows");
});

test("the catalog, one definition, a draft's save, and publish have their routes", async () => {
  const fetchMock = stubJson({});
  await registryCatalog();
  await getWorkflowDef("bug fix");
  await saveDraft("bug fix", { type: "story", yaml: "a: 1", prompts: { p: "x" } });
  await publishWorkflow("bug fix");
  expect(callOf(fetchMock, 0).url).toBe("/v1/workflows/catalog");
  expect(callOf(fetchMock, 1).url).toBe("/v1/workflows/bug%20fix");
  expect(callOf(fetchMock, 2)).toMatchObject({ url: "/v1/workflows/drafts/bug%20fix", method: "PUT", body: { type: "story", yaml: "a: 1", prompts: { p: "x" } } });
  expect(callOf(fetchMock, 3)).toMatchObject({ url: "/v1/workflows/bug%20fix/publish", method: "POST" });
});

test("deleteDraft answers nothing", async () => {
  const fetchMock = stubJson(null, { status: 204 });
  await expect(deleteDraft("d")).resolves.toBeUndefined();
  expect(callOf(fetchMock)).toMatchObject({ url: "/v1/workflows/drafts/d", method: "DELETE" });
});

test("validateWorkflow returns the problems, and sends the prompts only when given", async () => {
  const fetchMock = stubJson({ problems: ["no start"] });
  await expect(validateWorkflow("a: 1")).resolves.toEqual(["no start"]);
  await validateWorkflow("a: 1", { p: "x" });
  expect(callOf(fetchMock, 0)).toMatchObject({ url: "/v1/workflows/validate", method: "POST", body: { yaml: "a: 1" } });
  expect(callOf(fetchMock, 1).body).toEqual({ yaml: "a: 1", prompts: { p: "x" } });
});
