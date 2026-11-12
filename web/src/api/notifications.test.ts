import { afterEach, expect, test, vi } from "vitest";
import { callOf, stubJson } from "@/test-utils/fetch-stub";
import { disablePush, enablePush, notificationState, sendTest, setKinds } from "./notifications";

afterEach(() => vi.unstubAllGlobals());

function fakeRegistration(existing: { endpoint: string; unsubscribe: () => Promise<boolean> } | null = null) {
  const subscription = { toJSON: () => ({ endpoint: "https://push.example/abc", keys: { p256dh: "P", auth: "A" } }) };
  const subscribe = vi.fn(async () => subscription);
  const reg = { pushManager: { subscribe, getSubscription: vi.fn(async () => existing) } };
  return { reg: reg as unknown as ServiceWorkerRegistration, subscribe };
}

function permission(answer: NotificationPermission) {
  vi.stubGlobal("Notification", { requestPermission: vi.fn(async () => answer) });
}

test("enablePush asks permission, subscribes with the key as bytes and posts the subscription", async () => {
  permission("granted");
  const f = stubJson({}, { status: 204 });
  const { reg, subscribe } = fakeRegistration();
  await enablePush(reg, "AQID"); // base64url of bytes 1, 2, 3
  const options = (subscribe.mock.calls[0] as unknown as [{ userVisibleOnly: boolean; applicationServerKey: Uint8Array }])[0];
  expect(options.userVisibleOnly).toBe(true);
  expect(Array.from(options.applicationServerKey)).toEqual([1, 2, 3]);
  const call = callOf(f);
  expect(call.url).toBe("/v1/notifications/subscriptions");
  expect(call.method).toBe("POST");
  expect(call.body).toEqual({ endpoint: "https://push.example/abc", keys: { p256dh: "P", auth: "A" } });
});

test("enablePush decodes a base64url key (with - and _) to its exact bytes", async () => {
  permission("granted");
  stubJson({}, { status: 204 });
  const { reg, subscribe } = fakeRegistration();
  await enablePush(reg, "-_8APg"); // bytes 251, 255, 0, 62
  const options = (subscribe.mock.calls[0] as unknown as [{ applicationServerKey: Uint8Array }])[0];
  expect(options.applicationServerKey).toEqual(new Uint8Array([251, 255, 0, 62]));
});

test("a denied permission throws and posts nothing", async () => {
  permission("denied");
  const f = stubJson({}, { status: 204 });
  const { reg, subscribe } = fakeRegistration();
  await expect(enablePush(reg, "AQID")).rejects.toThrow(/blocked/);
  expect(subscribe).not.toHaveBeenCalled();
  expect(f).not.toHaveBeenCalled();
});

test("disablePush unsubscribes, then deletes the device", async () => {
  const f = stubJson({}, { status: 204 });
  const order: string[] = [];
  const unsubscribe = vi.fn(async () => {
    order.push("unsubscribe");
    return true;
  });
  f.mockImplementation(async () => {
    order.push("delete");
    return new Response(null, { status: 204 });
  });
  await disablePush(fakeRegistration({ endpoint: "https://push.example/abc", unsubscribe }).reg);
  expect(order).toEqual(["unsubscribe", "delete"]);
  expect(callOf(f)).toMatchObject({ url: "/v1/notifications/subscriptions", method: "DELETE", body: { endpoint: "https://push.example/abc" } });
});

test("disablePush with nothing subscribed does nothing", async () => {
  const f = stubJson({}, { status: 204 });
  await disablePush(fakeRegistration().reg);
  expect(f).not.toHaveBeenCalled();
});

test("state, kinds and the test push use their routes", async () => {
  const f = stubJson({ key: "K", kinds: { chat_reply: true }, devices: 1 });
  expect(await notificationState()).toEqual({ key: "K", kinds: { chat_reply: true }, devices: 1 });
  expect(callOf(f).url).toBe("/v1/notifications");

  const put = stubJson({}, { status: 204 });
  await setKinds({ chat_reply: false });
  expect(callOf(put)).toMatchObject({ url: "/v1/notifications/kinds", method: "PUT", body: { chat_reply: false } });

  const test_ = stubJson({ sent: 2 });
  expect(await sendTest()).toBe(2);
  expect(callOf(test_)).toMatchObject({ url: "/v1/notifications/test", method: "POST" });
});
