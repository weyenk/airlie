// The service worker's client side: register it, tell the page when a new build waits, and swap to it on request.

import { ref } from "vue";

/** True while a new build is installed and waiting behind the one running. */
export const updateReady = ref(false);

let current: ServiceWorkerRegistration | null = null;

function watch(registration: ServiceWorkerRegistration, nav: Navigator): void {
  const track = (worker: ServiceWorker | null): void => {
    if (!worker) return;
    worker.addEventListener("statechange", () => {
      // "installed" with a controller already in charge is an update; the first install is not
      if (worker.state === "installed" && nav.serviceWorker.controller) updateReady.value = true;
    });
  };
  if (registration.waiting && nav.serviceWorker.controller) updateReady.value = true;
  track(registration.installing);
  registration.addEventListener("updatefound", () => track(registration.installing));
}

/** Register /sw.js; null when this browser has no service workers or registering fails. */
export async function registerServiceWorker(nav: Navigator = navigator): Promise<ServiceWorkerRegistration | null> {
  if (!("serviceWorker" in nav) || !nav.serviceWorker) return null;
  try {
    const registration = await nav.serviceWorker.register("/sw.js", { scope: "/" });
    current = registration;
    watch(registration, nav);
    return registration;
  } catch {
    return null;
  }
}

/** Let the waiting build take over, and reload once it controls the page. */
export function applyUpdate(nav: Navigator = navigator): void {
  const waiting = current?.waiting;
  if (!waiting) return;
  nav.serviceWorker.addEventListener("controllerchange", () => location.reload(), { once: true });
  waiting.postMessage({ type: "SKIP_WAITING" });
}
