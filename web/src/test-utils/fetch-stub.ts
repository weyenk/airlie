// Test helper: a fetch that answers with fixed JSON, and a readable view of the calls it got.

export type Call = { url: string; method: string; body: unknown; headers: Record<string, string> };

export function stubJson(body: unknown = {}, init?: ResponseInit) {
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
    init?.status === 204 ? new Response(null, init) : Response.json(body, init),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

export function callOf(fetchMock: ReturnType<typeof stubJson>, n = 0): Call {
  const [url, init] = fetchMock.mock.calls[n];
  const raw = init?.body;
  return {
    url,
    method: init?.method ?? "GET",
    body: typeof raw === "string" ? JSON.parse(raw) : raw,
    headers: (init?.headers ?? {}) as Record<string, string>,
  };
}
