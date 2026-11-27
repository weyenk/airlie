import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { ApiError } from "@/api";
import { invitedBy, invitedYou } from "@/lib/operatorCopy";
import { useSessionStore } from "@/stores/session";
import JoinScreen from "./JoinScreen.vue";

const api = vi.hoisted(() => ({ peekInvite: vi.fn(), passkeyRegister: vi.fn(), passkeysSupported: vi.fn() }));
vi.mock("@/api/auth", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/auth")>()), ...api }));

async function open(token = "tok%2Fen", operator?: string) {
  // Answers per route: the copy read (`/v1/auth/operator`) must not swallow the session answer the store's
  // `load` reads after a passkey is created. No `operator_label` is an older server, i.e. the fallback.
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url === "/v1/auth/operator"
        ? Response.json(operator === undefined ? {} : { operator_label: operator })
        : Response.json({ name: "Marcus", role: "member", via: "session" }),
    ),
  );
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
  const { w } = await open(undefined, "Ada");
  expect(api.peekInvite).toHaveBeenCalledWith("tok/en"); // the path's token, decoded
  expect(w.get("h1").text()).toBe("You're invited");
  expect(w.text()).toContain("Marcus");
  // "Ada", not the default: the screen must show the operator the server named, not a value it shares with
  // the helper's own fallback. Both sides here read that configured label, so a drift in either is caught.
  expect(w.text()).toContain(invitedBy("Ada"));
  expect(w.text()).toContain(invitedYou("Ada"));
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
  expect(w.get("[role=alert]").text()).toBe("Invites work once and for a day. Ask your administrator for a new one.");
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
  [new TypeError("Failed to fetch"), "Can't reach airlie from this browser. Check you're connected the way this install is usually reached, and open it again at http://localhost:3000."],
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

// ---- who the copy names (auth.operator_label) ----

test("the invite page names the sender, and an organization's name reads as well as a person's", async () => {
  // "invited by <label>" is the sentence the label is actually for: it says who sent this link. The old copy
  // also hung the install's hardware on the label, which an organization's name cannot carry.
  const { w } = await open("tok%2Fen", "Acme Inc");
  expect(w.text()).toContain("invited by Acme Inc");
  expect(w.text()).toContain("Acme Inc invited you to airlie. Create a passkey to sign in here from now on.");
  expect(w.text()).not.toContain("undefined");
  expect(w.text()).not.toMatch(/Acme Inc'/);
});

test("a stale link names whoever issues invites here", async () => {
  api.peekInvite.mockRejectedValue(new ApiError(410, "invite expired or already used"));
  const { w } = await open("tok", "Dana");
  expect(w.get("[role=alert]").text()).toBe("Invites work once and for a day. Ask Dana for a new one.");
});

test("with no label configured the invite page still renders finished English", async () => {
  // The guard that matters for a default install: the key is absent, and the sentences must still be complete.
  // Literals, not the helpers — this is the shipped-default path, so comparing against the helper would only
  // prove the two share a fallback, not that the fallback is finished English.
  const { w } = await open();
  expect(w.text()).toContain("invited by your administrator");
  expect(w.text()).toContain("your administrator invited you to airlie. Create a passkey to sign in here from now on.");
  expect(w.text()).not.toContain("undefined");
});
