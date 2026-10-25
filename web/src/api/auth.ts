// The sign-in and people routes (`/v1/auth`), and the passkey ceremonies. The server speaks py_webauthn's JSON
// (bytes as base64url). Current Safari and Chrome convert it themselves (parseCreationOptionsFromJSON, toJSON);
// the helpers here cover the rest. Sign-in calls are `quiet401`: a refused passkey is not "you were signed out".

import { request, setKey } from "./index";

export type Role = "admin" | "member";

export type Me = { user_id: string; name: string; role: Role; via: string };

/** A person. `credentials` (how many passkeys) comes with the admin's list only. */
export type User = { id: string; name: string; login: string | null; role: Role; created_at: number; credentials?: number };

/** A link to hand a person (`/join/<token>`), when it lapses (epoch seconds), and who it is for. */
export type Invite = { url: string; expires_at: number; user: User };

/** Admin only. */
export function listUsers(init: { quiet401?: boolean } = {}): Promise<User[]> {
  return request<User[]>("/v1/auth/users", init);
}

/** Sign in with the admin key: a call that needs it, answered with the key, then kept. A refused key is an
 * ApiError with status 401 and is not kept. */
export async function signInWithKey(key: string): Promise<void> {
  await request("/v1/models", { key, quiet401: true });
  setKey(key);
}

/** Who an invite is for (the registration options name them), without using it up. A used or expired invite is an
 * ApiError with status 410. */
export async function peekInvite(invite: string): Promise<string> {
  const options = await request<Json>("/v1/auth/register/options", { method: "POST", body: { invite }, quiet401: true, key: "" });
  return String(options.user?.displayName || options.user?.name || "");
}

export function createInvite(body: { name: string; role: Role; login?: string | null }, init: { quiet401?: boolean } = {}): Promise<Invite> {
  return request<Invite>("/v1/auth/invites", { method: "POST", body, ...init });
}

/** A fresh link for an existing person: a lapsed invite, or a passkey on another device. */
export function inviteAgain(userId: string, init: { quiet401?: boolean } = {}): Promise<Invite> {
  return request<Invite>(`/v1/auth/users/${encodeURIComponent(userId)}/invites`, { method: "POST", ...init });
}

export function deleteUser(userId: string, init: { quiet401?: boolean } = {}): Promise<void> {
  return request<void>(`/v1/auth/users/${encodeURIComponent(userId)}`, { method: "DELETE", ...init });
}

// ---- passkeys (WebAuthn) ----

export function bufToB64url(buf: ArrayBuffer): string {
  let s = "";
  for (const b of new Uint8Array(buf)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function b64urlToBuf(text: string): ArrayBuffer {
  const b64 = text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any -- WebAuthn's JSON is the server library's

function withBuffers(options: Json, kind: "create" | "get"): Json {
  const o: Json = { ...options, challenge: b64urlToBuf(options.challenge) };
  if (kind === "create") o.user = { ...options.user, id: b64urlToBuf(options.user.id) };
  for (const k of ["excludeCredentials", "allowCredentials"]) {
    if (options[k]) o[k] = options[k].map((c: Json) => ({ ...c, id: b64urlToBuf(c.id) }));
  }
  return o;
}

function creationOptions(json: Json): PublicKeyCredentialCreationOptions {
  const P = (globalThis as Json).PublicKeyCredential;
  return (P?.parseCreationOptionsFromJSON ? P.parseCreationOptionsFromJSON(json) : withBuffers(json, "create")) as PublicKeyCredentialCreationOptions;
}

function requestOptions(json: Json): PublicKeyCredentialRequestOptions {
  const P = (globalThis as Json).PublicKeyCredential;
  return (P?.parseRequestOptionsFromJSON ? P.parseRequestOptionsFromJSON(json) : withBuffers(json, "get")) as PublicKeyCredentialRequestOptions;
}

/** A credential as JSON the server verifies (what `toJSON()` gives where it exists). */
export function credentialToJSON(cred: PublicKeyCredential): Json {
  if (typeof (cred as Json).toJSON === "function") return (cred as Json).toJSON();
  const r = cred.response as Json;
  const response: Json = { clientDataJSON: bufToB64url(r.clientDataJSON) };
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

export function passkeysSupported(): boolean {
  return Boolean((globalThis as Json).PublicKeyCredential && navigator.credentials?.create);
}

/** Register a passkey from an invite token and sign in; resolves with the person. A used or expired invite is an
 * ApiError with status 410. */
export async function passkeyRegister(invite: string): Promise<User> {
  const options = await request<Json>("/v1/auth/register/options", { method: "POST", body: { invite }, quiet401: true, key: "" });
  const cred = (await navigator.credentials.create({ publicKey: creationOptions(options) })) as PublicKeyCredential;
  const out = await request<{ user: User }>("/v1/auth/register/verify", {
    method: "POST",
    body: { invite, credential: credentialToJSON(cred) },
    quiet401: true,
    key: "",
  });
  return out.user;
}

/** Sign in with a passkey already on this device (or synced to it); resolves with the person. */
export async function passkeyLogin(): Promise<User> {
  const options = await request<Json>("/v1/auth/login/options", { method: "POST", quiet401: true, key: "" });
  const cred = (await navigator.credentials.get({ publicKey: requestOptions(options) })) as PublicKeyCredential;
  const out = await request<{ user: User }>("/v1/auth/login/verify", {
    method: "POST",
    body: { credential: credentialToJSON(cred) },
    quiet401: true,
    key: "",
  });
  return out.user;
}
