import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import NotificationsSheet from "./NotificationsSheet.vue";
import * as api from "@/api/notifications";

vi.mock("@/api/notifications", async (importActual) => ({
  ...(await importActual<typeof import("@/api/notifications")>()),
  notificationState: vi.fn(),
  thisDeviceSubscribed: vi.fn(async () => false),
  currentRegistration: vi.fn(async () => ({ id: "reg" })),
  enablePush: vi.fn(async () => undefined),
  disablePush: vi.fn(async () => undefined),
  setKinds: vi.fn(async () => undefined),
  sendTest: vi.fn(async () => 1),
}));

const KINDS = { chat_reply: true, model_ready: false, confirmation: true, story_blocked: true, story_published: false, story_mockups: true };

function agent(ua: string, standalone?: boolean) {
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(ua);
  Object.defineProperty(navigator, "standalone", { value: standalone, configurable: true });
}

beforeEach(() => {
  vi.mocked(api.notificationState).mockResolvedValue({ key: "KEY", kinds: { ...KINDS }, devices: 0 });
  agent("Mozilla/5.0 (Macintosh)");
});
afterEach(() => {
  vi.restoreAllMocks();
  Reflect.deleteProperty(navigator, "standalone");
  document.body.innerHTML = "";
});

async function open() {
  const w = mount(NotificationsSheet, { props: { open: true }, attachTo: document.body });
  await flushPromises();
  return w;
}

const buttons = () => [...document.body.querySelectorAll("button")];
const buttonNamed = (name: string) => buttons().find((b) => b.textContent?.trim() === name);

test("on iOS outside the installed app it says to add airlie to the Home Screen and offers no enable button", async () => {
  agent("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)", false);
  await open();
  expect(document.body.textContent).toContain("Add airlie to your Home Screen to get notifications");
  expect(buttonNamed("Turn on notifications")).toBeUndefined();
  expect(api.notificationState).not.toHaveBeenCalled();
});

test("with no key it says push is off on the Studio", async () => {
  vi.mocked(api.notificationState).mockResolvedValue({ key: null, kinds: { ...KINDS }, devices: 0 });
  await open();
  expect(document.body.textContent).toContain("Push is off on the Studio");
  expect(buttonNamed("Turn on notifications")).toBeUndefined();
});

test("it shows loading, then a toggle per kind with its label, and Send a test", async () => {
  let release!: (s: api.NotificationState) => void;
  vi.mocked(api.notificationState).mockReturnValue(new Promise((r) => (release = r)));
  const w = mount(NotificationsSheet, { props: { open: true }, attachTo: document.body });
  await flushPromises();
  expect(document.body.textContent).toContain("Loading");
  release({ key: "KEY", kinds: { ...KINDS }, devices: 0 });
  await flushPromises();
  const boxes = [...document.body.querySelectorAll<HTMLInputElement>("input[type=checkbox]")];
  expect(boxes.map((b) => b.closest("label")?.textContent?.trim())).toEqual([
    "A chat reply is ready",
    "A model or VM is ready",
    "Something needs your OK",
    "A story is blocked",
    "A story is published",
    "Mockups to pick",
  ]);
  expect(boxes.map((b) => b.checked)).toEqual([true, false, true, true, false, true]);
  expect(buttonNamed("Turn on notifications")).toBeDefined();
  expect(buttonNamed("Send a test")).toBeDefined();
  w.unmount();
});

test("turning on calls enablePush with the Studio's key from the click", async () => {
  await open();
  buttonNamed("Turn on notifications")!.click();
  await flushPromises();
  expect(api.enablePush).toHaveBeenCalledWith({ id: "reg" }, "KEY");
  expect(buttonNamed("Turn off on this device")).toBeDefined();
});

test("a refused permission shows the error", async () => {
  vi.mocked(api.enablePush).mockRejectedValueOnce(new Error("Notifications are blocked"));
  await open();
  buttonNamed("Turn on notifications")!.click();
  await flushPromises();
  expect(document.body.querySelector("[role=alert]")?.textContent).toContain("Notifications are blocked");
});

test("a toggle sends just that kind, and Send a test reports how many devices", async () => {
  await open();
  const box = document.body.querySelectorAll<HTMLInputElement>("input[type=checkbox]")[1];
  box.checked = true;
  box.dispatchEvent(new Event("change"));
  await flushPromises();
  expect(api.setKinds).toHaveBeenCalledWith({ model_ready: true });
  buttonNamed("Send a test")!.click();
  await flushPromises();
  expect(document.body.textContent).toContain("Sent to 1 device.");
});

test("a failing load shows the error", async () => {
  vi.mocked(api.notificationState).mockRejectedValue(new Error("HTTP 500"));
  await open();
  expect(document.body.querySelector("[role=alert]")?.textContent).toContain("HTTP 500");
});
