import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { ApiError } from "@/api";
import type { User } from "@/api/auth";
import { answerConfirm, confirmState } from "@/composables/useConfirm";
import { useSessionStore } from "@/stores/session";
import AdminScreen from "./AdminScreen.vue";

const api = vi.hoisted(() => ({ listUsers: vi.fn(), createInvite: vi.fn(), inviteAgain: vi.fn(), deleteUser: vi.fn() }));
vi.mock("@/api/auth", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/api/auth")>()), ...api }));

const NOW = 1_800_000_000;
const NOAH: User = { id: "u1", name: "Noah", login: "noah@github", role: "admin", created_at: NOW - 30 * 86400, credentials: 2 };
const JESS: User = { id: "u2", name: "Jess", login: null, role: "member", created_at: NOW - 20 * 86400, credentials: 1 };
const MARCUS: User = { id: "u3", name: "Marcus", login: "marcus@github", role: "member", created_at: NOW - 2 * 3600, credentials: 0 };

async function open(users: User[] = [NOAH, JESS, MARCUS]) {
  api.listUsers.mockResolvedValue(users);
  useSessionStore().me = { user_id: "u1", name: "Noah", role: "admin", via: "session" };
  const w = mount(AdminScreen, { attachTo: document.body });
  await flushPromises();
  return w;
}
type Wrapper = Awaited<ReturnType<typeof open>>;
const row = (w: Wrapper, name: string) => w.get(`[data-person="${name}"]`);
const clickButton = async (scope: { findAll(s: string): { text(): string; trigger(e: string): Promise<void> }[] }, label: string) => {
  const b = scope.findAll("button").find((x) => x.text() === label);
  if (!b) throw new Error(`no ${label} button`);
  await b.trigger("click");
  await flushPromises();
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW * 1000);
  setActivePinia(createPinia());
  for (const f of Object.values(api)) f.mockReset();
  api.createInvite.mockResolvedValue({ url: "/#/join/tok123", expires_at: NOW + 86400, user: { ...MARCUS, credentials: undefined } });
  api.inviteAgain.mockResolvedValue({ url: "/#/join/again", expires_at: NOW + 86400, user: JESS });
  api.deleteUser.mockResolvedValue(undefined);
  answerConfirm(false);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

test("each person shows role, passkeys, since when, Tailscale login and whether an invite waits", async () => {
  const w = await open();
  expect(w.get("[data-count]").text()).toBe("3");
  const noah = row(w, "Noah").text();
  expect(noah).toContain("you");
  expect(noah).toContain("admin");
  expect(noah).toContain("2 passkeys");
  expect(noah).toContain("noah@github");
  expect(noah).toMatch(/since [A-Z][a-z]{2} \d+/);
  expect(row(w, "Jess").text()).toContain("1 passkey");
  expect(row(w, "Jess").text()).toContain("member");
  expect(row(w, "Jess").text()).not.toContain("invite waiting");
  const marcus = row(w, "Marcus").text();
  expect(marcus).toContain("invite waiting");
  expect(marcus).toContain("invited 2 h ago");
  expect(marcus).toContain("marcus@github");
});

test("you cannot revoke yourself; everyone else can be revoked or given a new link", async () => {
  const w = await open();
  expect(row(w, "Noah").findAll("button").map((b) => b.text())).toEqual(["New link"]);
  expect(row(w, "Jess").findAll("button").map((b) => b.text())).toEqual(["New link", "Revoke"]);
});

test("names and logins are text, never markup", async () => {
  const w = await open([{ ...NOAH, name: "<img src=x onerror=alert(1)>", login: "<b>x</b>" }]);
  expect(w.find("img").exists()).toBe(false);
  expect(w.find("b").exists()).toBe(false);
  expect(w.text()).toContain("<img src=x onerror=alert(1)>");
  expect(w.text()).toContain("<b>x</b>");
});

test("a member is told only admins can see who is here", async () => {
  api.listUsers.mockRejectedValue(new ApiError(403, "admins only"));
  useSessionStore().me = { name: "Jess", role: "member", via: "session" };
  const w = mount(AdminScreen, { attachTo: document.body });
  await flushPromises();
  expect(w.text()).toContain("Only admins can see who's here.");
  expect(w.find("form").exists()).toBe(false);
});

test("an unreachable server says so", async () => {
  api.listUsers.mockRejectedValue(new TypeError("Failed to fetch"));
  const w = mount(AdminScreen, { attachTo: document.body });
  await flushPromises();
  expect(w.get("[role=alert]").text()).toBe("Can't reach airlie: Failed to fetch");
});

test("inviting sends name, role and login, then shows the link in a box with Copy link", async () => {
  const w = await open();
  await w.get("#invite-name").setValue("  Marcus ");
  await w.get("[data-role=admin]").trigger("click");
  await w.get("#invite-login").setValue(" marcus@github ");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(api.createInvite).toHaveBeenCalledWith({ name: "Marcus", role: "admin", login: "marcus@github" });
  const box = w.get("[data-link-box]");
  expect(box.text()).toContain("Link for Marcus. It works once, for a day.");
  expect(w.get("[data-invite-url]").text()).toBe(new URL("/#/join/tok123", location.href).href);
  expect(box.findAll("button").map((b) => b.text())).toEqual(["Copy link"]);
  expect((w.get("#invite-name").element as HTMLInputElement).value).toBe("");
  expect(w.text()).not.toContain("no longer signed in by itself"); // there were people already
});

test("an invite without a login leaves the login out", async () => {
  const w = await open();
  await w.get("#invite-name").setValue("Marcus");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(api.createInvite).toHaveBeenCalledWith({ name: "Marcus", role: "member", login: null });
});

test("Copy link copies the link", async () => {
  const writeText = vi.fn(async () => {});
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
  const w = await open();
  await clickButton(row(w, "Jess"), "New link");
  await clickButton(w.get("[data-link-box]"), "Copy link");
  expect(writeText).toHaveBeenCalledWith(new URL("/#/join/again", location.href).href);
  expect(w.get("[data-link-box]").text()).toContain("Copied");
});

test("Copy link falls back to the browser's copy command where there is no clipboard", async () => {
  vi.stubGlobal("navigator", { ...navigator, clipboard: undefined });
  const exec = vi.fn(() => true);
  (document as unknown as { execCommand: unknown }).execCommand = exec;
  const w = await open();
  await clickButton(row(w, "Jess"), "New link");
  await clickButton(w.get("[data-link-box]"), "Copy link");
  expect(exec).toHaveBeenCalledWith("copy");
  expect(w.get("[data-link-box]").text()).toContain("Copied");
});

test("New link shows a fresh link for that person", async () => {
  const w = await open();
  await clickButton(row(w, "Jess"), "New link");
  expect(api.inviteAgain).toHaveBeenCalledWith("u2");
  expect(w.get("[data-link-box]").text()).toContain("Link for Jess.");
});

test("Revoke asks first with its own wording, and does nothing on No", async () => {
  const w = await open();
  await clickButton(row(w, "Jess"), "Revoke");
  expect(confirmState.open).toBe(true);
  expect(confirmState.title).toBe("Revoke Jess?");
  expect(confirmState.confirmLabel).toBe("Revoke");
  expect(confirmState.message).toBe("Remove Jess? Their passkeys and sessions end now.");
  expect(api.deleteUser).not.toHaveBeenCalled();
  answerConfirm(false);
  await flushPromises();
  expect(api.deleteUser).not.toHaveBeenCalled();
});

test("Revoke, once confirmed, removes the person and reloads the list", async () => {
  const w = await open();
  await clickButton(row(w, "Jess"), "Revoke");
  api.listUsers.mockResolvedValue([NOAH, MARCUS]);
  answerConfirm(true);
  await flushPromises();
  expect(api.deleteUser).toHaveBeenCalledWith("u2");
  expect(w.find('[data-person="Jess"]').exists()).toBe(false);
  expect(w.get("[data-count]").text()).toBe("2");
});

test("a failed invite says why; a non-admin's says only admins can", async () => {
  const w = await open();
  api.createInvite.mockRejectedValueOnce(new ApiError(409, "a user with login 'x' already exists"));
  await w.get("#invite-name").setValue("X");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(w.get("[data-invite-error]").text()).toBe("a user with login 'x' already exists");
  api.createInvite.mockRejectedValueOnce(new ApiError(403, "admins only"));
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(w.get("[data-invite-error]").text()).toBe("Only admins can invite.");
});

test("inviting the first person hands the Mac over, and says so beside the link", async () => {
  const w = await open([]);
  expect(w.text()).toContain("Nobody yet");
  await w.get("#invite-name").setValue("Noah");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(w.get("[data-link-box]").text()).toContain(
    "That was the first person, so this Mac is no longer signed in by itself. Open the link to create a passkey, then come back here as that person.",
  );
  expect(w.find("[data-invite-url]").exists()).toBe(true);
});

test("a refused Revoke says why beside the list: the server's sentence, or a friendly line for a 403", async () => {
  const w = await open();
  api.deleteUser.mockRejectedValueOnce(new ApiError(409, "cannot remove the last admin"));
  await clickButton(row(w, "Jess"), "Revoke");
  answerConfirm(true);
  await flushPromises();
  expect(w.get("[data-list-error]").text()).toBe("cannot remove the last admin");
  expect(w.find("[data-invite-error]").exists()).toBe(false);
  expect(w.find('[data-person="Jess"]').exists()).toBe(true);

  api.deleteUser.mockRejectedValueOnce(new ApiError(403, "admins only"));
  await clickButton(row(w, "Jess"), "Revoke");
  answerConfirm(true);
  await flushPromises();
  expect(w.get("[data-list-error]").text()).toBe("Only admins can revoke.");

  api.deleteUser.mockResolvedValueOnce(undefined);
  await clickButton(row(w, "Jess"), "Revoke");
  answerConfirm(true);
  await flushPromises();
  expect(w.find("[data-list-error]").exists()).toBe(false); // a good try clears the old complaint
});

test("a failed New link says why beside the list", async () => {
  const w = await open();
  api.inviteAgain.mockRejectedValue(new ApiError(404, "no such user"));
  await clickButton(row(w, "Jess"), "New link");
  expect(w.get("[data-list-error]").text()).toBe("no such user");
});

test("revoking the person a link was for clears that link, and only theirs", async () => {
  const w = await open();
  await clickButton(row(w, "Jess"), "New link");
  expect(w.find("[data-link-box]").exists()).toBe(true);
  await clickButton(row(w, "Marcus"), "Revoke");
  answerConfirm(true);
  await flushPromises();
  expect(w.find("[data-link-box]").exists()).toBe(true); // Jess's link is still good
  await clickButton(row(w, "Jess"), "Revoke");
  answerConfirm(true);
  await flushPromises();
  expect(w.find("[data-link-box]").exists()).toBe(false);
});

test("an invited person appears in the list at once, without a reload", async () => {
  const w = await open([NOAH]);
  api.listUsers.mockResolvedValue([NOAH, { ...MARCUS, credentials: 0 }]);
  await w.get("#invite-name").setValue("Marcus");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(row(w, "Marcus").text()).toContain("invite waiting");
  expect(w.get("[data-count]").text()).toBe("2");
});

test("after the hand-over the list stays quiet: no empty line, no error from the 401 that follows", async () => {
  const w = await open([]);
  api.listUsers.mockRejectedValue(new ApiError(401, "not signed in"));
  await w.get("#invite-name").setValue("Noah");
  await w.get("form").trigger("submit");
  await flushPromises();
  expect(w.find("[data-link-box]").exists()).toBe(true);
  expect(w.text()).not.toContain("Nobody yet");
  expect(w.text()).not.toContain("Can't reach airlie");
  expect(api.listUsers).toHaveBeenLastCalledWith({ quiet401: true });
});
