import { ApiError, KEY, getKey, request } from "./api";

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function stubFetch(response: Response) {
  const fetchMock = vi.fn(async () => response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

test("the key lives where today's app keeps it", () => {
  expect(KEY).toBe("airlie.key");
  expect(getKey()).toBe("");
  localStorage.setItem("airlie.key", "k1");
  expect(getKey()).toBe("k1");
});

test("a localStorage that throws is no key", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  expect(getKey()).toBe("");
});

test("request sends the cookie and the stored key, and returns the JSON", async () => {
  localStorage.setItem("airlie.key", "k1");
  const fetchMock = stubFetch(Response.json({ ok: true }));
  const signal = new AbortController().signal;

  await expect(request<{ ok: boolean }>("/v1/stories", { signal })).resolves.toEqual({ ok: true });

  const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(path).toBe("/v1/stories");
  expect(init.method).toBe("GET");
  expect(init.credentials).toBe("same-origin");
  expect(init.headers).toEqual({ Authorization: "Bearer k1" });
  expect(init.signal).toBe(signal);
  expect(init.body).toBeUndefined();
});

test("request sends no Authorization header without a key, and a body as JSON", async () => {
  const fetchMock = stubFetch(Response.json({}));

  await request("/v1/board", { method: "POST", body: { a: 1 } });

  const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  expect(init.method).toBe("POST");
  expect(init.headers).toEqual({ "Content-Type": "application/json" });
  expect(init.body).toBe('{"a":1}');
});

test("a failed request is an ApiError with the status and the server's detail", async () => {
  stubFetch(Response.json({ detail: "no such story" }, { status: 404 }));

  const error = await request("/v1/stories/x").catch((e: unknown) => e);

  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({ status: 404, message: "no such story" });
});

test("a failed request with no detail says its status", async () => {
  stubFetch(new Response("", { status: 502 }));

  await expect(request("/v1/stories")).rejects.toMatchObject({ status: 502, message: "HTTP 502" });
});

test("a 204 has no body to read", async () => {
  stubFetch(new Response(null, { status: 204 }));

  await expect(request("/v1/board/AIR-1", { method: "DELETE" })).resolves.toBeUndefined();
});
