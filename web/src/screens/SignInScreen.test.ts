import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { ApiError } from "@/api";
import { useSessionStore } from "@/stores/session";
import SignInScreen from "./SignInScreen.vue";

const api = vi.hoisted(() => ({ passkeyLogin: vi.fn(), signInWithKey: vi.fn(), passkeysSupported: vi.fn() }));
vi.mock("@/api/auth", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/auth")>()), ...api }));

const NOBODY = () => Response.json({ error: "not signed in" }, { status: 401 });

async function open(url = "/signin?next=/board", me: () => Response = NOBODY) {
  vi.stubGlobal("fetch", vi.fn(async () => me()));
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

beforeEach(() => {
  setActivePinia(createPinia());
  api.passkeysSupported.mockReset().mockReturnValue(true);
  api.passkeyLogin.mockReset().mockResolvedValue({ id: "u1" });
  api.signInWithKey.mockReset().mockResolvedValue(undefined);
});
afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

test("the page offers this device, the invite pointer and the admin key (folded)", async () => {
  const { w } = await open();
  expect(w.get("h1").text()).toBe("Sign in");
  expect(w.text()).toContain("Use this device");
  expect(w.text()).toContain("No passkey yet? Open the invite link Noah sent you on this device.");
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
  [Object.assign(new Error("x"), { name: "NotAllowedError" }), null],
  [Object.assign(new Error("x"), { name: "AbortError" }), null],
  [new ApiError(401, "sign-in failed"), "That passkey isn't known here. Ask Noah for an invite."],
  [new TypeError("Failed to fetch"), "Can't reach airlie. Is it running, and are you on Noah's network?"],
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
  const { w, router } = await open();
  api.signInWithKey.mockRejectedValue(new ApiError(401, "no"));
  await w.get("input[type=password]").setValue("nope");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(alertText(w)).toBe("That key isn't valid. Ask Noah for a new one.");
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

test("an unreachable server on arrival says to check Tailscale", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/signin", component: SignInScreen }] });
  await router.push("/signin");
  const w = mount(SignInScreen, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  expect(alertText(w)).toBe("Can't reach airlie. Turn on Tailscale and check you're on Noah's network.");
});
