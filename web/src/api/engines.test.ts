import { callOf, stubJson } from "@/test-utils/fetch-stub";
import { listEngines } from "./engines";

afterEach(() => vi.unstubAllGlobals());

test("the engines list uses the engines route", async () => {
  const fetchMock = stubJson([{ name: "gemma", state: "ready", roles: ["chat"] }]);

  await expect(listEngines()).resolves.toEqual([{ name: "gemma", state: "ready", roles: ["chat"] }]);

  expect(callOf(fetchMock, 0).url).toBe("/v1/airlie/engines");
});
