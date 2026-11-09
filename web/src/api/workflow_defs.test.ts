import { ApiError } from "@/api";
import { callOf, stubJson } from "@/test-utils/fetch-stub";
import catalogJson from "@/test-utils/catalog.json";
import { adoptWorkflow, deleteDraft, getWorkflowDef, listWorkflowDefs, publishWorkflow, registryCatalog, saveDraft, usedBy, validateWorkflow } from "./workflow_defs";
import type { Catalog } from "./workflow_defs";

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

test("the recorded server payload is a Catalog, and the client returns what the server sends", async () => {
  // the payload's JSON widens each check's `formats` to a union of object shapes; as the maps they are, the
  // fixture must satisfy `Catalog`, so a renamed or dropped field fails to compile here
  const recorded = {
    ...catalogJson,
    checks: catalogJson.checks.map((c) => ({ ...c, formats: Object.fromEntries(Object.entries(c.formats)) as Record<string, string> })),
  } satisfies Catalog;
  stubJson(recorded);
  const got = await registryCatalog();
  expect(got.formats.length).toBeGreaterThan(0);
  expect(got.kinds.length).toBeGreaterThan(0);
  const check = got.checks.find((c) => c.name === "story.intake")!;
  expect(check).toMatchObject({ inputs: ["card"], every_run: true, args: [] });
  expect(got.meetings[0]).toEqual({ roles: ["product", "architect", "tester"], inputs: ["story", "layers"] });
});

test("adoptWorkflow posts to the story's adopt-workflow route and returns the move", async () => {
  const fetchMock = stubJson({ key: "AIR 1", name: "bug", from: "story", to: "bug" });
  await expect(adoptWorkflow("AIR 1")).resolves.toEqual({ key: "AIR 1", name: "bug", from: "story", to: "bug" });
  expect(callOf(fetchMock)).toMatchObject({ url: "/v1/airlie/stories/AIR%201/adopt-workflow", method: "POST" });
});

test("usedBy reads a 409's used_by detail, and nothing else", () => {
  expect(usedBy(new ApiError(409, JSON.stringify({ used_by: ["story", "layer"] })))).toEqual(["story", "layer"]);
  expect(usedBy(new ApiError(409, "plain text"))).toBeNull();
  expect(usedBy(new ApiError(409, JSON.stringify({ used_by: [1] })))).toBeNull();
  expect(usedBy(new ApiError(422, JSON.stringify({ used_by: ["story"] })))).toBeNull();
});
