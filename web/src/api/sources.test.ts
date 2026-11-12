import { ApiError } from "./index";
import { personRepositories, pollLink, startLink, unlink } from "./sources";
import { callOf, stubJson } from "@/test-utils/fetch-stub";

afterEach(() => vi.unstubAllGlobals());

const VIEW: Awaited<ReturnType<typeof personRepositories>> = {
  sources: [{ name: "github_app", title: "GitHub", linked: false, login: null, repositories: [], error: null, listed_at: null }],
  extras: [],
  extra_candidates: ["local/only"],
};

test("personRepositories asks with refresh when told", async () => {
  const fetchMock = stubJson(VIEW);
  expect(await personRepositories()).toEqual(VIEW);
  expect(callOf(fetchMock).url).toBe("/v1/airlie/chat/repositories?refresh=false");
  await personRepositories(true);
  expect(callOf(fetchMock, 1).url).toBe("/v1/airlie/chat/repositories?refresh=true");
});

test("startLink posts the source", async () => {
  const body = { link_id: "lid", user_code: "ABCD-1234", verification_uri: "https://github.com/login/device", expires_at: 9, interval: 5 };
  const fetchMock = stubJson(body, { status: 201 });
  expect(await startLink("github_app")).toEqual(body);
  expect(callOf(fetchMock)).toMatchObject({ method: "POST", url: "/v1/airlie/chat/sources/github_app/link" });
});

test("pollLink posts the link id", async () => {
  const fetchMock = stubJson({ state: "pending", interval: 5 }, { status: 202 });
  expect(await pollLink("github_app", "lid")).toEqual({ state: "pending", interval: 5 });
  expect(callOf(fetchMock)).toMatchObject({ method: "POST", url: "/v1/airlie/chat/sources/github_app/link/lid" });
});

test("unlink deletes the link", async () => {
  const fetchMock = stubJson({}, { status: 204 });
  await unlink("github_app");
  expect(callOf(fetchMock)).toMatchObject({ method: "DELETE", url: "/v1/airlie/chat/sources/github_app/link" });
});

test("pollLink rejects with the server's status", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json({ error: "you declined on GitHub" }, { status: 403 })),
  );
  await expect(pollLink("github_app", "lid")).rejects.toMatchObject({ status: 403, message: "you declined on GitHub" } satisfies Partial<ApiError>);
});
