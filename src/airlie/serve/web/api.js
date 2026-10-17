// The API client: the serve key, JSON requests to /v1 and the chat stream.
// A 401 anywhere is an AuthError; the shell's handler sends the person to Sign in.

const KEY = "airlie.key";

export class AuthError extends Error {
  constructor() {
    super("not signed in");
    this.name = "AuthError";
  }
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
  }
}

export class StreamCut extends Error {
  /** The stream ended without `[DONE]`: `partial` is what arrived. */
  constructor(partial) {
    super("airlie stopped answering");
    this.name = "StreamCut";
    this.partial = partial;
  }
}

export function getKey() {
  try {
    return localStorage.getItem(KEY) || "";
  } catch {
    return "";
  }
}

export function setKey(key) {
  try {
    localStorage.setItem(KEY, key);
  } catch {
    /* private mode: the key lives for this page only */
  }
}

export function clearKey() {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* nothing stored */
  }
}

let onAuthError = () => {};

/** `fn` runs on every 401 (except a sign-in check, which passes its own `key`). */
export function setAuthHandler(fn) {
  onAuthError = fn;
}

function headers(key, json) {
  const h = {};
  const k = key === undefined ? getKey() : key;
  if (k) h.Authorization = `Bearer ${k}`;
  if (json) h["Content-Type"] = "application/json";
  return h;
}

async function detailOf(r) {
  const text = await r.text().catch(() => "");
  try {
    const j = JSON.parse(text);
    const d = j.detail ?? j.error ?? j;
    return typeof d === "string" ? d : JSON.stringify(d);
  } catch {
    return text;
  }
}

/** JSON in, JSON out. `key` overrides the stored key (the sign-in check). */
export async function request(path, { method = "GET", body, key, signal } = {}) {
  const r = await fetch(path, {
    method,
    headers: headers(key, body !== undefined),
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (r.status === 401) {
    const e = new AuthError();
    if (key === undefined) onAuthError(e);
    throw e;
  }
  if (!r.ok) throw new ApiError(r.status, await detailOf(r));
  if (r.status === 204) return null;
  return r.json();
}

/** The `data:` payloads of an SSE body, one string per event, in order. */
export async function* sseEvents(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let data = [];
  const flush = () => {
    const out = data.length ? data.join("\n") : null;
    data = [];
    return out;
  };
  for (;;) {
    const { value, done } = await reader.read();
    buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      let line = buffer.slice(0, nl);
      buffer = buffer.slice(nl + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      if (line === "") {
        const ev = flush();
        if (ev !== null) yield ev;
      } else if (line.startsWith("data:")) {
        data.push(line.slice(5).replace(/^ /, ""));
      }
    }
    if (done) break;
  }
  if (buffer.startsWith("data:")) data.push(buffer.slice(5).replace(/^ /, ""));
  const last = flush();
  if (last !== null) yield last;
}

/** POST `body` to `path` and call `onDelta(text)` for every content delta. Resolves with the whole text on
 * `[DONE]`; rejects with StreamCut (its `partial` is what arrived) when the body ends before that. */
export async function stream(path, body, onDelta, { signal } = {}) {
  const r = await fetch(path, {
    method: "POST",
    headers: headers(undefined, true),
    body: JSON.stringify(body),
    signal,
  });
  if (r.status === 401) {
    const e = new AuthError();
    onAuthError(e);
    throw e;
  }
  if (!r.ok) throw new ApiError(r.status, await detailOf(r));
  let partial = "";
  let finished = false;
  try {
    for await (const ev of sseEvents(r.body)) {
      if (ev === "[DONE]") {
        finished = true;
        break;
      }
      let obj;
      try {
        obj = JSON.parse(ev);
      } catch {
        continue;
      }
      const text = obj?.choices?.[0]?.delta?.content;
      if (text) {
        partial += text;
        onDelta(text);
      }
    }
  } catch (e) {
    if (e?.name === "AbortError") throw e;
    throw new StreamCut(partial);
  }
  if (!finished) throw new StreamCut(partial);
  return partial;
}
