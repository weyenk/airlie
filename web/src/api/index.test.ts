import { ApiError, KEY, getKey, onUnauthorized, queryString, request, signalUnauthorized } from "./index";

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

test("a 422 with a string detail reads as that string", async () => {
  stubFetch(Response.json({ detail: "unknown stage 'x'; use one of new, intake" }, { status: 422 }));

  await expect(request("/v1/airlie/stories?stage=x")).rejects.toMatchObject({
    status: 422,
    message: "unknown stage 'x'; use one of new, intake",
  });
});

test("a 422 with FastAPI's validation list reads as its messages, field first", async () => {
  stubFetch(
    Response.json(
      {
        detail: [
          { type: "missing", loc: ["body", "title"], msg: "Field required", input: {} },
          { type: "enum", loc: ["body", "level"], msg: "Input should be 'story'" },
          { type: "value_error", loc: [], msg: "bad body" },
        ],
      },
      { status: 422 },
    ),
  );

  await expect(request("/v1/board/items", { method: "POST", body: {} })).rejects.toMatchObject({
    status: 422,
    message: "title: Field required; level: Input should be 'story'; bad body",
  });
});

test("queryString leaves out what is not given", () => {
  expect(queryString({ a: "x y", b: undefined, c: null, d: 0, e: false, f: true })).toBe("?a=x+y&d=0&e=false&f=true");
  expect(queryString({ a: undefined })).toBe("");
});

test("a 401 from a helper-signalled call reaches the unauthorized handler once", () => {
  const heard = vi.fn();
  onUnauthorized(heard);
  signalUnauthorized();
  onUnauthorized(undefined);
  expect(heard).toHaveBeenCalledTimes(1);
});
