import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { useSessionStore } from "./session";

beforeEach(() => setActivePinia(createPinia()));
afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "serviceWorker");
});

function subscribedDevice() {
  const unsubscribe = vi.fn(async () => true);
  const subscription = { endpoint: "https://push.example/abc", unsubscribe };
  const reg = { pushManager: { getSubscription: vi.fn(async () => subscription) } };
  Object.defineProperty(navigator, "serviceWorker", { value: { getRegistration: async () => reg }, configurable: true });
}

test("signOut removes this device's subscription before the logout", async () => {
  subscribedDevice();
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method} ${url}`);
      return new Response(null, { status: 204 });
    }),
  );
  await useSessionStore().signOut();
  expect(calls).toEqual(["DELETE /v1/notifications/subscriptions", "POST /v1/auth/logout"]);
});

test("signOut still logs out when removing the device fails", async () => {
  subscribedDevice();
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method} ${url}`);
      return url.includes("notifications") ? new Response("boom", { status: 500 }) : new Response(null, { status: 204 });
    }),
  );
  await useSessionStore().signOut();
  expect(calls).toContain("POST /v1/auth/logout");
});

test("signOut without a subscription only logs out", async () => {
  const f = vi.fn(async () => new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", f);
  await useSessionStore().signOut();
  expect(f.mock.calls.map((c) => (c as unknown as [string])[0])).toEqual(["/v1/auth/logout"]);
});
