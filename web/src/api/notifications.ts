// Push notifications (`/v1/notifications`): this device's subscription and the kinds a person wants. The browser asks
// for permission only from a tap, so `enablePush` is only ever called from a click handler.

import { b64urlToBuf } from "./auth";
import { request } from "./index";

export type NotificationState = { key: string | null; kinds: Record<string, boolean>; devices: number };

export function notificationState(): Promise<NotificationState> {
  return request<NotificationState>("/v1/notifications");
}

/** Ask permission, subscribe this browser with the Studio's key, and tell the Studio. A refusal throws and posts nothing. */
export async function enablePush(reg: ServiceWorkerRegistration, key: string): Promise<void> {
  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new Error("Notifications are blocked for airlie in this browser's settings.");
  const subscription = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: new Uint8Array(b64urlToBuf(key)) });
  const { endpoint, keys } = subscription.toJSON() as { endpoint: string; keys: { p256dh: string; auth: string } };
  await request("/v1/notifications/subscriptions", { method: "POST", body: { endpoint, keys } });
}

/** Unsubscribe this browser, then tell the Studio to forget the device. Nothing subscribed is nothing to do. */
export async function disablePush(reg: ServiceWorkerRegistration): Promise<void> {
  const subscription = await reg.pushManager.getSubscription();
  if (!subscription) return;
  const endpoint = subscription.endpoint;
  await subscription.unsubscribe();
  await request("/v1/notifications/subscriptions", { method: "DELETE", body: { endpoint } });
}

export function setKinds(kinds: Record<string, boolean>): Promise<void> {
  return request<void>("/v1/notifications/kinds", { method: "PUT", body: kinds });
}

/** Send this person's devices a test push; how many it reached. */
export async function sendTest(): Promise<number> {
  return (await request<{ sent: number }>("/v1/notifications/test", { method: "POST" })).sent;
}

/** The worker's registration, or null where this browser has no service workers. */
export async function currentRegistration(): Promise<ServiceWorkerRegistration | null> {
  if (!("serviceWorker" in navigator) || !navigator.serviceWorker) return null;
  return (await navigator.serviceWorker.getRegistration()) ?? null;
}

/** Whether this browser holds a push subscription (a failure to ask is "no"). */
export async function thisDeviceSubscribed(): Promise<boolean> {
  try {
    const reg = await currentRegistration();
    return Boolean(reg && (await reg.pushManager.getSubscription()));
  } catch {
    return false;
  }
}

/** Sign-out: take this device off the Studio's list while the session still works. Never throws. */
export async function removeThisDevice(): Promise<void> {
  try {
    const reg = await currentRegistration();
    if (reg && (await reg.pushManager.getSubscription())) await disablePush(reg);
  } catch {
    // signing out matters more than tidying the device list; the Studio drops gone endpoints itself
  }
}
