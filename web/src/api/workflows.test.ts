import { callOf, stubJson } from "@/test-utils/fetch-stub";
import {
  cancelJob,
  listJobs,
  listRunning,
  listStories,
  repoNames,
  runFlow,
  runJob,
  startRun,
  startStory,
  storyFlow,
  storyJob,
} from "./workflows";

afterEach(() => vi.unstubAllGlobals());

test("listStories sends the query it is given and returns the page whole", async () => {
  const page = { stories: [{ key: "AIR-1" }], total: 1, page: 2, size: 10 };
  const fetchMock = stubJson(page);

  await expect(listStories({ page: 2, size: 10, stage: "build", blocked: false, q: "x y", sort: "key" })).resolves.toEqual(page);
  expect(callOf(fetchMock).url).toBe("/v1/airlie/stories?page=2&size=10&stage=build&blocked=false&q=x+y&sort=key");

  await listStories({});
  expect(callOf(fetchMock, 1).url).toBe("/v1/airlie/stories");
});

test("running and jobs unwrap their lists", async () => {
  const fetchMock = stubJson({ running: [1], jobs: [2] });

  await expect(listRunning()).resolves.toEqual([1]);
  await expect(listJobs()).resolves.toEqual([2]);

  expect(callOf(fetchMock, 0).url).toBe("/v1/airlie/running");
  expect(callOf(fetchMock, 1).url).toBe("/v1/airlie/jobs");
});

test("the story and run flows and their job details have their own routes", async () => {
  const fetchMock = stubJson({});

  await storyFlow("AIR-1");
  await storyJob("AIR-1", "layer-01", "build");
  await runFlow("20261024-abc");
  await runJob("20261024-abc", "stage", "job 1");

  expect(fetchMock.mock.calls.map((c) => c[0])).toEqual([
    "/v1/airlie/stories/AIR-1/flow",
    "/v1/airlie/stories/AIR-1/flow/layer-01/build",
    "/v1/airlie/runs/20261024-abc/flow",
    "/v1/airlie/runs/20261024-abc/flow/stage/job%201",
  ]);
});

test("repoNames unwraps the names", async () => {
  const fetchMock = stubJson({ repos: ["a/b"] });
  await expect(repoNames()).resolves.toEqual(["a/b"]);
  expect(callOf(fetchMock).url).toBe("/v1/airlie/repos/names");
});

test("starting a story or a run POSTs the request and returns the job; cancelling DELETEs it", async () => {
  const fetchMock = stubJson({ job_id: "story-AIR-1", kind: "story" }, { status: 202 });

  await expect(startStory({ card: "AIR-1" })).resolves.toEqual({ job_id: "story-AIR-1", kind: "story" });
  await startStory({ text: "do it", repo: "a/b" });
  await startRun({ goal: "g", repo: "/r", plan: { p: 1 } });
  await cancelJob("story-AIR-1");

  expect(callOf(fetchMock, 0)).toMatchObject({ url: "/v1/airlie/stories", method: "POST", body: { card: "AIR-1" } });
  expect(callOf(fetchMock, 1).body).toEqual({ text: "do it", repo: "a/b" });
  expect(callOf(fetchMock, 2)).toMatchObject({ url: "/v1/airlie/runs", method: "POST", body: { goal: "g", repo: "/r", plan: { p: 1 } } });
  expect(callOf(fetchMock, 3)).toMatchObject({ url: "/v1/airlie/jobs/story-AIR-1", method: "DELETE" });
});
