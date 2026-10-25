import { callOf, stubJson } from "@/test-utils/fetch-stub";
import {
  bufToB64url,
  b64urlToBuf,
  createInvite,
  deleteUser,
  inviteAgain,
  listUsers,
  logout,
  me,
  passkeyLogin,
  passkeyRegister,
  peekInvite,
  signInWithKey,
} from "./auth";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

test("me, users and logout use the auth routes", async () => {
  const fetchMock = stubJson([{ id: "u1" }]);

  await me();
  await expect(listUsers()).resolves.toEqual([{ id: "u1" }]);
  await logout();

  expect(callOf(fetchMock, 0).url).toBe("/v1/auth/me");
  expect(callOf(fetchMock, 1).url).toBe("/v1/auth/users");
  expect(callOf(fetchMock, 2)).toMatchObject({ url: "/v1/auth/logout", method: "POST" });
});

test("invites, user invites and delete user", async () => {
  const fetchMock = stubJson({ url: "/#/join/t", expires_at: 1, user: { id: "u1" } }, { status: 201 });

  await createInvite({ name: "Ann", role: "member", login: "ann" });
  await inviteAgain("u 1");
  expect(callOf(fetchMock, 0)).toMatchObject({
    url: "/v1/auth/invites",
    method: "POST",
    body: { name: "Ann", role: "member", login: "ann" },
  });
  expect(callOf(fetchMock, 1)).toMatchObject({ url: "/v1/auth/users/u%201/invites", method: "POST" });

  const deleted = stubJson(null, { status: 204 });
  await deleteUser("u1");
  expect(callOf(deleted)).toMatchObject({ url: "/v1/auth/users/u1", method: "DELETE" });
});

test("base64url round-trips bytes", () => {
  const bytes = new Uint8Array([250, 251, 252, 0, 1, 2, 255]);
  expect(new Uint8Array(b64urlToBuf(bufToB64url(bytes.buffer)))).toEqual(bytes);
  expect(bufToB64url(bytes.buffer)).not.toMatch(/[+/=]/);
});

test("registering a passkey sends the invite, then the credential, and returns the person", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(Response.json({ challenge: "AQID", user: { id: "BAUG", name: "Ann" } }))
    .mockResolvedValueOnce(Response.json({ user: { id: "u1", name: "Ann" } }, { status: 201 }));
  vi.stubGlobal("fetch", fetchMock);
  const create = vi.fn(async () => ({ toJSON: () => ({ id: "cred" }) }));
  vi.stubGlobal("PublicKeyCredential", { parseCreationOptionsFromJSON: (j: unknown) => ({ parsed: j }) });
  vi.stubGlobal("navigator", { credentials: { create } });

  await expect(passkeyRegister("tok")).resolves.toEqual({ id: "u1", name: "Ann" });

  expect(create).toHaveBeenCalledWith({ publicKey: { parsed: { challenge: "AQID", user: { id: "BAUG", name: "Ann" } } } });
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ invite: "tok" });
  expect(fetchMock.mock.calls[1][0]).toBe("/v1/auth/register/verify");
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ invite: "tok", credential: { id: "cred" } });
});

test("a used invite is a 410 ApiError", async () => {
  stubJson({ error: "this invite was used or has expired" }, { status: 410 });
  await expect(passkeyRegister("tok")).rejects.toMatchObject({ status: 410 });
});

test("signing in with a passkey asks for options, signs, and returns the person", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(Response.json({ challenge: "AQID" }))
    .mockResolvedValueOnce(Response.json({ user: { id: "u1" } }));
  vi.stubGlobal("fetch", fetchMock);
  const get = vi.fn(async () => ({ toJSON: () => ({ id: "cred" }) }));
  vi.stubGlobal("PublicKeyCredential", { parseRequestOptionsFromJSON: (j: unknown) => ({ parsed: j }) });
  vi.stubGlobal("navigator", { credentials: { get } });

  await expect(passkeyLogin()).resolves.toEqual({ id: "u1" });

  expect(fetchMock.mock.calls[0][0]).toBe("/v1/auth/login/options");
  expect(fetchMock.mock.calls[1][0]).toBe("/v1/auth/login/verify");
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ credential: { id: "cred" } });
});

test("the admin key is checked with the key itself, and kept only when accepted", async () => {
  localStorage.clear();
  const ok = stubJson({ data: [] });
  await signInWithKey("airlie_k_1");
  expect(callOf(ok)).toMatchObject({ url: "/v1/models", headers: { Authorization: "Bearer airlie_k_1" } });
  expect(localStorage.getItem("airlie.key")).toBe("airlie_k_1");

  localStorage.clear();
  stubJson({ error: "bad key" }, { status: 401 });
  await expect(signInWithKey("nope")).rejects.toMatchObject({ status: 401 });
  expect(localStorage.getItem("airlie.key")).toBeNull();
});

test("peeking at an invite names who it is for, and sends no key; a lapsed invite is a 410", async () => {
  localStorage.setItem("airlie.key", "stored");
  const fetchMock = stubJson({ challenge: "AQID", user: { id: "x", name: "ann", displayName: "Ann" } });
  await expect(peekInvite("tok")).resolves.toBe("Ann");
  expect(callOf(fetchMock)).toMatchObject({ url: "/v1/auth/register/options", method: "POST", body: { invite: "tok" }, headers: { "Content-Type": "application/json" } });
  expect(callOf(fetchMock).headers).not.toHaveProperty("Authorization");

  stubJson({ error: "invite expired or already used" }, { status: 410 });
  await expect(peekInvite("old")).rejects.toMatchObject({ status: 410, message: "invite expired or already used" });
  localStorage.clear();
});
