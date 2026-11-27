import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { ApiError } from "@/api";
import { badKey, invitePointer } from "@/lib/operatorCopy";
import { useSessionStore } from "@/stores/session";
import SignInScreen from "./SignInScreen.vue";

const api = vi.hoisted(() => ({
  passkeyLogin: vi.fn(),
  signInWithKey: vi.fn(),
  passkeysSupported: vi.fn(),
  signInOptions: vi.fn(),
}));
vi.mock("@/api/auth", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/auth")>()), ...api }));

const NOBODY = () => Response.json({ error: "not signed in" }, { status: 401 });
/** No IdP here — what a default install answers, and what a server that cannot answer is read as. */
const NO_SSO = () => Promise.resolve({ oidc: false });
const SSO_ON = () => Promise.resolve({ oidc: true });

/** Answers per route, so the copy read (`/v1/auth/operator`, which the screen asks for on arrival) can speak
 * while `/v1/auth/me` still answers `me`. An `operator_label` here is a configured install; leaving it out is
 * an older server, which leaves the copy on its fallback. */
function routes(me: () => Response, operator?: string) {
  return vi.fn(async (url: string) => {
    if (url === "/v1/auth/operator") return Response.json(operator === undefined ? {} : { operator_label: operator });
    return me();
  });
}

async function open(url = "/signin?next=/board", me: () => Response = NOBODY, options = NO_SSO, operator?: string) {
  vi.stubGlobal("fetch", routes(me, operator));
  api.signInOptions.mockImplementation(options);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/", component: { template: "<i>chat</i>" } },
      { path: "/board", component: { template: "<i>board</i>" } },
      { path: "/signin", component: SignInScreen },
    ],
  });
  await router.push(url);
  const w = mount(SignInScreen, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return { w, router };
}
const alertText = (w: Awaited<ReturnType<typeof open>>["w"]) => w.find("[role=alert]").text();
/** Present when this install has an IdP, absent when it has none — so `exists()` is the whole of the first
 * check, and `attributes("href")` is only ever read on a link that is there. */
const ssoLink = (w: Awaited<ReturnType<typeof open>>["w"]) => w.find("[data-sso]");

beforeEach(() => {
  setActivePinia(createPinia());
  api.passkeysSupported.mockReset().mockReturnValue(true);
  api.passkeyLogin.mockReset().mockResolvedValue({ id: "u1" });
  api.signInWithKey.mockReset().mockResolvedValue(undefined);
  api.signInOptions.mockReset().mockImplementation(NO_SSO);
});
afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

test("the page offers this device, the invite pointer and the admin key (folded)", async () => {
  // A label distinct from the shipped default: the screen must render what `/v1/auth/operator` said, not a
  // value it shares with `invitePointer()`'s own fallback. Defaulting both to the same label would let a
  // screen that ignored the server still pass, which is the exact drift the old literal caught.
  const { w } = await open(undefined, undefined, undefined, "Ada");
  expect(w.get("h1").text()).toBe("Sign in");
  expect(w.text()).toContain("Use this device");
  expect(w.text()).toContain(invitePointer("Ada"));
  expect(w.get("[data-invite-pointer]").text()).toBe(invitePointer("Ada"));
  expect(w.get("details").attributes("open")).toBeUndefined();
  expect(w.get("details summary").text()).toContain("Sign in with the admin key");
  expect(w.find("[role=alert]").exists()).toBe(false);
});

test("a passkey signs in and lands on the page asked for", async () => {
  const { w, router } = await open();
  await w.get("[data-passkey]").trigger("click");
  await flushPromises();
  expect(api.passkeyLogin).toHaveBeenCalledOnce();
  expect(router.currentRoute.value.fullPath).toBe("/board");
});

test("a sign-in with no next lands on the chat page", async () => {
  const { w, router } = await open("/signin");
  await w.get("[data-passkey]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/");
});

test.each([["//evil.com"], ["https://evil.com/x"], ["/\\evil.com"]])("next=%s is not followed", async (next) => {
  const { w, router } = await open(`/signin?next=${encodeURIComponent(next)}`);
  await w.get("[data-passkey]").trigger("click");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/");
});

test.each([
  [
    Object.assign(new Error("x"), { name: "NotAllowedError" }),
    "Passkey didn't complete on http://localhost:3000. Open airlie at the same address you used when you created the passkey (this install's usual https:// address, or http://127.0.0.1:8090 when you created it locally), then try again.",
  ],
  [Object.assign(new Error("x"), { name: "AbortError" }), null],
  [
    new ApiError(401, "sign-in failed"),
    // A literal, not `unknownPasskey()`: this `test.each` table is a message-identity map, and calling the
    // same helper the screen calls would compare one source against itself.
    "That passkey isn't known here. Ask your administrator for an invite.",
  ],
  [new TypeError("Failed to fetch"), `Can't reach airlie from this browser. Check you're connected the way this install is usually reached, and open it again at http://localhost:3000.`],
  [new Error("boom"), "Can't sign in: boom"],
])("a failed passkey (%s) says %s", async (error, message) => {
  const { w, router } = await open();
  api.passkeyLogin.mockRejectedValue(error);
  await w.get("[data-passkey]").trigger("click");
  await flushPromises();
  if (message) expect(alertText(w)).toBe(message);
  else expect(w.find("[role=alert]").exists()).toBe(false); // a cancelled prompt is not an error
  expect(router.currentRoute.value.path).toBe("/signin");
  expect((w.get("[data-passkey]").element as HTMLButtonElement).disabled).toBe(false);
});

test("a browser without passkeys says so and cannot try", async () => {
  api.passkeysSupported.mockReturnValue(false);
  const { w } = await open();
  const button = w.get("[data-passkey]");
  expect(button.text()).toBe("Passkeys aren't available in this browser");
  expect((button.element as HTMLButtonElement).disabled).toBe(true);
});

test("the admin key signs in and lands on the page asked for", async () => {
  const { w, router } = await open();
  await w.get("input[type=password]").setValue("  airlie_k_abc  ");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(api.signInWithKey).toHaveBeenCalledWith("airlie_k_abc");
  expect(router.currentRoute.value.fullPath).toBe("/board");
});

test("an empty key asks nothing", async () => {
  const { w } = await open();
  await w.get("form").trigger("submit");
  expect(api.signInWithKey).not.toHaveBeenCalled();
});

test("a refused key says so and stays", async () => {
  const { w, router } = await open(undefined, undefined, undefined, "Ada");
  api.signInWithKey.mockRejectedValue(new ApiError(401, "no"));
  await w.get("input[type=password]").setValue("nope");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(alertText(w)).toBe(badKey("Ada"));
  expect(router.currentRoute.value.path).toBe("/signin");
});

test("an unreachable server on the key says so", async () => {
  const { w } = await open();
  api.signInWithKey.mockRejectedValue(new TypeError("Failed to fetch"));
  await w.get("input[type=password]").setValue("k");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(alertText(w)).toBe("Can't reach airlie: Failed to fetch");
});

test("someone already known (Tailscale, or a session) is handed straight on to next", async () => {
  const { router } = await open("/signin?next=/board", () => Response.json({ name: "Noah", role: "admin", via: "tailscale" }));
  expect(useSessionStore().me?.name).toBe("Noah");
  expect(router.currentRoute.value.fullPath).toBe("/board");
});

test("an unreachable server on arrival names an address, not a network to turn on", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/signin", component: SignInScreen }] });
  await router.push("/signin");
  const w = mount(SignInScreen, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  expect(alertText(w)).toBe(
    "Can't reach airlie from this browser. Open it at the address it usually answers — a Tailscale https://… address, or the host your own install runs on — and try again.",
  );
});

test("a second press while the first is working asks nothing more", async () => {
  const { w } = await open();
  let release: () => void = () => {};
  api.passkeyLogin.mockReturnValue(new Promise<void>((r) => (release = r)));
  await w.get("[data-passkey]").trigger("click");
  await w.get("[data-passkey]").trigger("click");
  expect(api.passkeyLogin).toHaveBeenCalledOnce();
  release();
  await flushPromises();
});

// ---- sign in with the company IdP ----

test("with no IdP configured the screen offers no SSO and looks as it did before", async () => {
  const { w } = await open();
  expect(ssoLink(w).exists()).toBe(false);
  expect(w.text()).not.toContain("Sign in with SSO");
  // and nothing else changed about the screen
  expect(w.text()).toContain("Use this device");
});

test("an options call that fails offers no SSO and says nothing about it", async () => {
  const { w } = await open("/signin", NOBODY, () => Promise.reject(new TypeError("Failed to fetch")));
  expect(ssoLink(w).exists()).toBe(false);
  // a sign-in screen whose only news is "can't reach the options endpoint" is a worse screen than a quiet one
  expect(w.find("[role=alert]").exists()).toBe(false);
});

test("with an IdP the screen offers SSO", async () => {
  const { w } = await open("/signin", NOBODY, SSO_ON);
  expect(ssoLink(w).text()).toContain("Sign in with SSO");
});

test("the SSO link is a browser navigation, not a fetch", async () => {
  const { w } = await open("/signin?next=/board", NOBODY, SSO_ON);
  const link = ssoLink(w);
  expect(link.element.tagName).toBe("A");
  expect(link.attributes("href")).toBe("/v1/auth/oidc/start?target=%2Fboard");
  // A fetch would follow the 302 to the IdP itself and fail as CORS. Nothing here may intercept the click.
  const before = (fetch as ReturnType<typeof vi.fn>).mock.calls.length;
  await link.trigger("click");
  expect((fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(before);
});

test("the SSO link carries the page asked for, so a sign-in resumes it", async () => {
  const { w } = await open("/signin?next=/board/AIR-17", NOBODY, SSO_ON);
  expect(ssoLink(w).attributes("href")).toBe("/v1/auth/oidc/start?target=%2Fboard%2FAIR-17");
});

test("a next that is not an app route sends SSO to the studio root", async () => {
  const { w } = await open(`/signin?next=${encodeURIComponent("//evil.com")}`, NOBODY, SSO_ON);
  expect(ssoLink(w).attributes("href")).toBe("/v1/auth/oidc/start");
});

test("with no next the SSO link asks for no target", async () => {
  const { w } = await open("/signin", NOBODY, SSO_ON);
  expect(ssoLink(w).attributes("href")).toBe("/v1/auth/oidc/start");
});

test("an unbound callback lands here and the screen says what to do about it", async () => {
  const { w } = await open("/signin?next=/board&oidc_unbound=1", NOBODY, SSO_ON);
  expect(alertText(w)).toBe(
    "Your company confirmed who you are, but this airlie account isn't linked to it yet. Ask an airlie administrator to link your account, then sign in again.",
  );
});

test("the unbound note does not appear without the flag", async () => {
  const { w } = await open("/signin?next=/board", NOBODY, SSO_ON);
  expect(w.find("[role=alert]").exists()).toBe(false);
});

test("the retry link keeps the flag, so a still-unlinked person lands back on the sentence", async () => {
  const { w } = await open("/signin?next=/board&oidc_unbound=1", NOBODY, SSO_ON);
  expect(ssoLink(w).attributes("href")).toBe("/v1/auth/oidc/start?target=%2Fboard&oidc_unbound=1");
});

test("the flag on the retry link is the flag and nothing else", async () => {
  // The value repeated is the one this page already read; a link that copied arbitrary query values into
  // /start's URL would forward anything a stranger put on the sign-in address to the IdP's authorize endpoint.
  const { w } = await open("/signin?next=/board&oidc_unbound=1&sub=user-123&iss=https%3A%2F%2Fid.acme.com", NOBODY, SSO_ON);
  const href = ssoLink(w).attributes("href");
  expect(href).toBe("/v1/auth/oidc/start?target=%2Fboard&oidc_unbound=1");
  expect(href).not.toContain("user-123");
  expect(href).not.toContain("acme.com");
});

test("an unbound note without an IdP still reads (a link was removed from under them)", async () => {
  const { w } = await open("/signin?oidc_unbound=1", NOBODY, NO_SSO);
  expect(alertText(w)).toContain("isn't linked to it yet");
  expect(ssoLink(w).exists()).toBe(false);
});

// ---- who the copy names (auth.operator_label) ----

test("the screen names the operator only where a name is the useful information", async () => {
  const { w } = await open("/signin", NOBODY, NO_SSO, "Acme Inc");
  // The subtitle describes the install, so it says nothing about who owns it — the old text produced
  // "The studio on Acme Inc' Mac Studio", both wrong about the hardware and broken as grammar.
  expect(w.text()).toContain("This airlie studio. Sign in as yourself.");
  expect(w.text()).toContain("No passkey yet? Open the invite link Acme Inc sent you on this device.");
  api.passkeyLogin.mockRejectedValue(new ApiError(401, "sign-in failed"));
  await w.get("[data-passkey]").trigger("click");
  await flushPromises();
  expect(alertText(w)).toBe("That passkey isn't known here. Ask Acme Inc for an invite.");
  expect(w.text()).not.toContain("Mac Studio");
});

test("with no label configured the screen still renders finished English", async () => {
  // The guard that matters for a default install: the key is absent (and an older server has no route to
  // answer at all), and what shows must be real sentences — not a blank, not a template, not a hole where the
  // name belonged.
  const { w } = await open();
  expect(w.text()).toContain("This airlie studio. Sign in as yourself.");
  // Literal, not `invitePointer()`: this is the one test where a helper call would compare the screen and the
  // helper against each other with nothing pinning that either matches what the copy is supposed to say. The
  // screen fell back to the shipped default here (no label configured), so this is the sentence that must show.
  expect(w.text()).toContain("No passkey yet? Open the invite link your administrator sent you on this device.");
  expect(w.text()).not.toContain("undefined");
  expect(w.text()).not.toContain("Mac Studio");
});

test("a server too old to answer for the operator keeps real sentences", async () => {
  // 404 from a server without the route: a sign-in screen must not print a hole where a name belongs.
  const { w } = await open("/signin", NOBODY, NO_SSO, undefined);
  expect(w.text()).toContain("This airlie studio.");
  expect(w.text()).toContain("No passkey yet? Open the invite link your administrator sent you on this device.");
});
