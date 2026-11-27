import { ApiError } from "@/api";
import { DEFAULT_OPERATOR_LABEL, fetchOperatorLabel, operatorLabel, setOperatorLabel } from "./operator";

type Call = { url: string; method: string; headers: Record<string, string> };

/** The next fetches answer `body`, and every one of them is recorded. */
function stubJson(...body: unknown[]): Call[] {
  const calls: Call[] = [];
  let n = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: { method?: string; headers?: Record<string, string> } = {}) => {
      calls.push({ url, method: init.method ?? "GET", headers: init.headers ?? {} });
      const next = body[n++] ?? body.at(-1);
      return next instanceof Error ? Promise.reject(next) : Response.json(next);
    }),
  );
  return calls;
}

beforeEach(() => setOperatorLabel(null));
afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

test("it is read from the auth route, with no key riding along", async () => {
  const calls = stubJson({ operator_label: "Dana" });

  await fetchOperatorLabel();

  // `key: ""` because the caller asking is the sign-in screen: whoever happens to hold an admin key in this
  // browser must not be the reason the copy names one operator over another.
  expect(calls[0]).toMatchObject({ url: "/v1/auth/operator", method: "GET" });
  expect(calls[0].headers.Authorization).toBeUndefined();
  expect(operatorLabel.value).toBe("Dana");
});

test("a 401 leaves the fallback and asks nobody to sign out", async () => {
  // A cold /signin is nobody, and the copy a person reads there is no reason to fire the app's 401 handler.
  localStorage.setItem("airlie.key", "airlie_k_stale");
  stubJson(new ApiError(401, "not signed in"));
  let fired = false;

  await fetchOperatorLabel().then(
    () => (fired = false),
    () => (fired = true),
  );

  expect(fired).toBe(false); // it resolves; nothing reached the caller as a failure
  expect(operatorLabel.value).toBe(DEFAULT_OPERATOR_LABEL);
});

test("a server too old to have the route leaves the fallback standing", async () => {
  stubJson(new TypeError("Failed to fetch"));
  await expect(fetchOperatorLabel()).resolves.toBeUndefined();
  expect(operatorLabel.value).toBe(DEFAULT_OPERATOR_LABEL);
});

test("an answer without the key leaves the fallback standing", async () => {
  stubJson({});
  await fetchOperatorLabel();
  expect(operatorLabel.value).toBe(DEFAULT_OPERATOR_LABEL);
});
