// The API client: JSON requests to /v1 with the session cookie, or the serve key where a person has one.
// Today's app keeps the key in localStorage under the same name, so a person signed in there stays signed in here.

export const KEY = "airlie.key";

export class ApiError extends Error {
  status: number;

  constructor(status: number, detail?: string) {
    super(detail || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
  }
}

export function getKey(): string {
  try {
    return localStorage.getItem(KEY) || "";
  } catch {
    // private mode or blocked storage: nobody has a key
    return "";
  }
}

/** The request headers that sign in by key; none when no key is stored (the cookie does it). */
export function authHeaders(): Record<string, string> {
  const key = getKey();
  return key ? { Authorization: `Bearer ${key}` } : {};
}

/** What an error answer says, as one line. Two 422 shapes come back: a string `detail` (the stories route's
 * unknown stage) and FastAPI's validation list of {loc, msg}, which reads as "field: message; ...". */
export function describeDetail(json: unknown): string {
  const object = json as { detail?: unknown; error?: unknown } | null;
  const detail = object?.detail ?? object?.error ?? json;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length && detail.every((d) => typeof d?.msg === "string")) {
    return detail
      .map((d: { loc?: unknown[]; msg: string }) => {
        const field = (d.loc ?? []).filter((part) => part !== "body" && part !== "query").join(".");
        return field ? `${field}: ${d.msg}` : d.msg;
      })
      .join("; ");
  }
  return JSON.stringify(detail);
}

/** The message of a failed answer whose body is `text` (JSON with a detail or error, or plain text). */
export function detailOfText(text: string): string {
  try {
    return describeDetail(JSON.parse(text));
  } catch {
    return text;
  }
}

async function detailOf(response: Response): Promise<string> {
  return detailOfText(await response.text().catch(() => ""));
}

/** `?a=1&b=x` from the params that are given (undefined and null are left out), or "" when none are. */
export function queryString(params: Record<string, string | number | boolean | null | undefined>): string {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== null) q.set(key, String(value));
  const text = q.toString();
  return text ? `?${text}` : "";
}

let unauthorizedHandler: (() => void) | undefined;

/** Who hears a 401 from any call: the session store, which sends the person to sign in. */
export function onUnauthorized(fn: (() => void) | undefined): void {
  unauthorizedHandler = fn;
}

/** Tell the unauthorized handler (calls that do not go through `request`: the chat stream, an upload). */
export function signalUnauthorized(): void {
  unauthorizedHandler?.();
}

/** JSON in, JSON out. A non-2xx answer is an ApiError. */
export async function request<T>(
  path: string,
  init: { method?: string; body?: unknown; signal?: AbortSignal; quiet401?: boolean } = {},
): Promise<T> {
  const headers = authHeaders();
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: init.method ?? "GET",
    headers,
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
    credentials: "same-origin",
    signal: init.signal,
  });
  if (response.status === 401 && !init.quiet401) unauthorizedHandler?.();
  if (!response.ok) throw new ApiError(response.status, await detailOf(response));
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
