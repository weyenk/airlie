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

async function detailOf(response: Response): Promise<string> {
  const text = await response.text().catch(() => "");
  try {
    const json = JSON.parse(text);
    const detail = json.detail ?? json.error ?? json;
    return typeof detail === "string" ? detail : JSON.stringify(detail);
  } catch {
    return text;
  }
}

let unauthorizedHandler: (() => void) | undefined;

/** Who hears a 401 from any call: the session store, which sends the person to sign in. */
export function onUnauthorized(fn: (() => void) | undefined): void {
  unauthorizedHandler = fn;
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
