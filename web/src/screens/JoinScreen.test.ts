import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { ApiError } from "@/api";
import { useSessionStore } from "@/stores/session";
import JoinScreen from "./JoinScreen.vue";

const api = vi.hoisted(() => ({ peekInvite: vi.fn(), passkeyRegister: vi.fn(), passkeysSupported: vi.fn() }));
vi.mock("@/api/auth", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/auth")>()), ...api }));

async function open(token = "tok%2Fen") {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ name: "Marcus", role: "member", via: "session" })));
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/", component: { template: "<i>chat</i>" } },
      { path: "/signin", component: { template: "<i>signin</i>" } },
      { path: "/join/:token", component: JoinScreen },
    ],
  });
  await router.push(`/join/${token}`);
  const w = mount(JoinScreen, { global: { plugins: [router] }, attachTo: document.body });
  await flushPromises();
  return { w, router };
}
const button = (w: Awaited<ReturnType<typeof open>>["w"]) => w.find("[data-create]");

beforeEach(() => {
  setActivePinia(createPinia());
  api.passkeysSupported.mockReset().mockReturnValue(true);
  api.peekInvite.mockReset().mockResolvedValue("Marcus");
  api.passkeyRegister.mockReset().mockResolvedValue({ id: "u1", name: "Marcus" });
});
afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

test("the invite is read from the path and names who it is for and who sent it", async () => {
  const { w } = await open();
  expect(api.peekInvite).toHaveBeenCalledWith("tok/en"); // the path's token, decoded
  expect(w.get("h1").text()).toBe("You're invited");
  expect(w.text()).toContain("Marcus");
  expect(w.text()).toContain("invited by Noah");
  expect(w.text()).toContain("Noah invited you to airlie. Create a passkey to sign in here from now on.");
  expect((button(w).element as HTMLButtonElement).disabled).toBe(false);
  expect(w.get("a").text()).toBe("Sign in");
});

test("a name is shown as text, never as markup", async () => {
  api.peekInvite.mockResolvedValue("<img src=x onerror=alert(1)>");
  const { w } = await open();
  expect(w.find('img[src="x"]').exists()).toBe(false);
  expect(w.text()).toContain("<img src=x onerror=alert(1)>");
});

test("creating the passkey signs in and goes to the chat page", async () => {
  const { w, router } = await open();
  await button(w).trigger("click");
  await flushPromises();
  expect(api.passkeyRegister).toHaveBeenCalledWith("tok/en");
  expect(useSessionStore().me?.name).toBe("Marcus");
  expect(router.currentRoute.value.fullPath).toBe("/");
});

test("an invite that is gone (410 on arrival) is the expired page", async () => {
  api.peekInvite.mockRejectedValue(new ApiError(410, "invite expired or already used"));
  const { w } = await open();
  expect(w.get("h1").text()).toBe("This link has expired");
  expect(w.get("[role=alert]").text()).toBe("Invites work once and for a day. Ask Noah for a new one.");
  expect(w.text()).toContain("Already have a passkey?");
  expect(button(w).exists()).toBe(false);
});

test("an invite used up while the prompt was open is the expired page too", async () => {
  api.passkeyRegister.mockRejectedValue(new ApiError(410, "gone"));
  const { w } = await open();
  await button(w).trigger("click");
  await flushPromises();
  expect(w.get("h1").text()).toBe("This link has expired");
});

test.each([
  [Object.assign(new Error("x"), { name: "NotAllowedError" }), null],
  [Object.assign(new Error("x"), { name: "InvalidStateError" }), "This device already has a passkey for airlie. Sign in with it instead."],
  [new TypeError("Failed to fetch"), "Can't reach airlie. Is it running, and are you on Noah's network?"],
  [new Error("boom"), "Couldn't create the passkey: boom"],
])("a failed passkey (%s) says %s and can be tried again", async (error, message) => {
  const { w, router } = await open();
  api.passkeyRegister.mockRejectedValue(error);
  await button(w).trigger("click");
  await flushPromises();
  if (message) expect(w.get("[role=alert]").text()).toBe(message);
  else expect(w.find("[role=alert]").exists()).toBe(false);
  expect((button(w).element as HTMLButtonElement).disabled).toBe(false);
  expect(router.currentRoute.value.path).toContain("/join/");
});

test("a browser that cannot make passkeys says where to open the link", async () => {
  api.passkeysSupported.mockReturnValue(false);
  const { w } = await open();
  expect(w.text()).toContain("This browser can't make passkeys. Open the link in Safari or Chrome on your phone or Mac.");
  expect((button(w).element as HTMLButtonElement).disabled).toBe(true);
});

test("a server that cannot be reached on arrival says so", async () => {
  api.peekInvite.mockRejectedValue(new TypeError("Failed to fetch"));
  const { w } = await open();
  expect(w.get("[role=alert]").text()).toBe("Can't reach airlie: Failed to fetch");
});

test("a second press while the first is working creates one passkey", async () => {
  const { w } = await open();
  let release: () => void = () => {};
  api.passkeyRegister.mockReturnValue(new Promise<void>((r) => (release = r)));
  await button(w).trigger("click");
  await button(w).trigger("click");
  expect(api.passkeyRegister).toHaveBeenCalledOnce();
  release();
  await flushPromises();
});
