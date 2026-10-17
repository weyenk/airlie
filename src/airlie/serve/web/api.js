// The API client: the serve key, JSON requests to /v1 and the chat stream, and the passkey ceremonies.
// Every request sends the session cookie (`credentials: "same-origin"`): a person signed in by Tailscale or a
// passkey has no key. A 401 anywhere is an AuthError; the shell's handler sends the person to Sign in.

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
    credentials: "same-origin",
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

/** Who the server says we are (`/v1/auth/me`), or null for nobody. Never triggers the sign-in handler. */
export async function whoami() {
  try {
    return await request("/v1/auth/me", { key: "" });
  } catch (e) {
    if (e?.name === "AuthError") return null;
    throw e;
  }
}

/** End this device's session (the cookie) and forget the key. */
export async function signOut() {
  clearKey();
  try {
    await request("/v1/auth/logout", { method: "POST", key: "" });
  } catch {
    /* already nobody */
  }
}

// ---- passkeys (WebAuthn) ----
// The server speaks py_webauthn's JSON (bytes as base64url). Current Safari and Chrome convert it themselves
// (PublicKeyCredential.parseCreationOptionsFromJSON / toJSON); these helpers cover the rest.

export function bufToB64url(buf) {
  let s = "";
  for (const b of new Uint8Array(buf)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function b64urlToBuf(text) {
  const b64 = text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

function withBuffers(options, kind) {
  const o = { ...options, challenge: b64urlToBuf(options.challenge) };
  if (kind === "create") o.user = { ...options.user, id: b64urlToBuf(options.user.id) };
  for (const k of ["excludeCredentials", "allowCredentials"]) {
    if (options[k]) o[k] = options[k].map((c) => ({ ...c, id: b64urlToBuf(c.id) }));
  }
  return o;
}

function creationOptions(json) {
  const P = globalThis.PublicKeyCredential;
  return P?.parseCreationOptionsFromJSON ? P.parseCreationOptionsFromJSON(json) : withBuffers(json, "create");
}

function requestOptions(json) {
  const P = globalThis.PublicKeyCredential;
  return P?.parseRequestOptionsFromJSON ? P.parseRequestOptionsFromJSON(json) : withBuffers(json, "get");
}

/** A credential as JSON the server verifies (what `toJSON()` gives where it exists). */
export function credentialToJSON(cred) {
  if (typeof cred.toJSON === "function") return cred.toJSON();
  const r = cred.response;
  const response = { clientDataJSON: bufToB64url(r.clientDataJSON) };
  if (r.attestationObject) {
    response.attestationObject = bufToB64url(r.attestationObject);
    response.transports = typeof r.getTransports === "function" ? r.getTransports() : [];
  } else {
    response.authenticatorData = bufToB64url(r.authenticatorData);
    response.signature = bufToB64url(r.signature);
    response.userHandle = r.userHandle ? bufToB64url(r.userHandle) : null;
  }
  return {
    id: cred.id,
    rawId: bufToB64url(cred.rawId),
    type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment ?? null,
    clientExtensionResults: cred.getClientExtensionResults?.() ?? {},
    response,
  };
}

export function passkeysSupported() {
  return Boolean(globalThis.PublicKeyCredential && navigator.credentials?.create);
}

/** Register a passkey from an invite token and sign in; resolves with the person. A used or expired invite is an
 * ApiError with status 410. */
export async function passkeyRegister(invite) {
  const options = await request("/v1/auth/register/options", { method: "POST", body: { invite }, key: "" });
  const cred = await navigator.credentials.create({ publicKey: creationOptions(options) });
  const out = await request("/v1/auth/register/verify", {
    method: "POST",
    body: { invite, credential: credentialToJSON(cred) },
    key: "",
  });
  return out.user;
}

/** Sign in with a passkey already on this device (or synced to it); resolves with the person. */
export async function passkeyLogin() {
  const options = await request("/v1/auth/login/options", { method: "POST", key: "" });
  const cred = await navigator.credentials.get({ publicKey: requestOptions(options) });
  const out = await request("/v1/auth/login/verify", { method: "POST", body: { credential: credentialToJSON(cred) }, key: "" });
  return out.user;
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

/** POST `body` to `path` and call `onDelta(text)` for every content delta (`onReasoning(text)` for a thinking
 * model's reasoning deltas). Resolves with the whole text on `[DONE]`; rejects with StreamCut (its `partial`
 * is what arrived) when the body ends before that. */
export async function stream(path, body, onDelta, { signal, onReasoning } = {}) {
  const r = await fetch(path, {
    method: "POST",
    headers: headers(undefined, true),
    body: JSON.stringify(body),
    credentials: "same-origin",
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
      const delta = obj?.choices?.[0]?.delta || {};
      if (delta.reasoning_content && onReasoning) onReasoning(delta.reasoning_content);
      if (delta.content) {
        partial += delta.content;
        onDelta(delta.content);
      }
    }
  } catch (e) {
    if (e?.name === "AbortError") throw e;
    throw new StreamCut(partial);
  }
  if (!finished) throw new StreamCut(partial);
  return partial;
}
