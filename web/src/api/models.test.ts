import { callOf, stubJson } from "@/test-utils/fetch-stub";
import { listModels, setModelAllowed } from "./models";

afterEach(() => vi.unstubAllGlobals());

test("the models list uses the models route", async () => {
  const rows = [{ id: "gemma", label: "Gemma", allowed: true, live: true }];
  const fetchMock = stubJson(rows);

  await expect(listModels()).resolves.toEqual(rows);

  expect(callOf(fetchMock, 0).url).toBe("/v1/airlie/models");
});

test("switching a model PATCHes its id, keeping slashes as path segments", async () => {
  const fetchMock = stubJson(null, { status: 204 });

  await setModelAllowed("org/my model", false);

  const call = callOf(fetchMock, 0);
  expect(call.url).toBe("/v1/airlie/models/org/my%20model");
  expect(call.method).toBe("PATCH");
  expect(call.body).toEqual({ allowed: false });
});
