import { beforeEach, expect, test, vi } from "vitest";
import { applyUpdate, registerServiceWorker, updateReady } from "./sw";

class Fake {
  listeners: Record<string, Array<() => void>> = {};
  addEventListener(type: string, h: () => void) {
    (this.listeners[type] ??= []).push(h);
  }
  fire(type: string) {
    (this.listeners[type] ?? []).forEach((h) => h());
  }
}

function setup(controller: object | null) {
  const worker = Object.assign(new Fake(), { state: "installing", postMessage: vi.fn() });
  const registration = Object.assign(new Fake(), { installing: null as unknown, waiting: null as unknown });
  const container = Object.assign(new Fake(), {
    controller,
    register: vi.fn(async () => registration),
  });
  return { worker, registration, container, nav: { serviceWorker: container } as unknown as Navigator };
}

beforeEach(() => {
  updateReady.value = false;
});

test("registers /sw.js at the root scope", async () => {
  const s = setup(null);
  expect(await registerServiceWorker(s.nav)).toBe(s.registration);
  expect(s.container.register).toHaveBeenCalledWith("/sw.js", { scope: "/" });
});

test("a navigator without service workers resolves null", async () => {
  expect(await registerServiceWorker({} as Navigator)).toBeNull();
});

test("a registration that fails resolves null", async () => {
  const s = setup(null);
  s.container.register.mockRejectedValueOnce(new Error("insecure"));
  expect(await registerServiceWorker(s.nav)).toBeNull();
});

test("an installed worker under a controller means an update is ready", async () => {
  const s = setup({});
  await registerServiceWorker(s.nav);
  s.registration.installing = s.worker;
  s.registration.fire("updatefound");
  expect(updateReady.value).toBe(false);
  s.worker.state = "installed";
  s.worker.fire("statechange");
  expect(updateReady.value).toBe(true);
});

test("the first install, with no controller, is not an update", async () => {
  const s = setup(null);
  await registerServiceWorker(s.nav);
  s.registration.installing = s.worker;
  s.registration.fire("updatefound");
  s.worker.state = "installed";
  s.worker.fire("statechange");
  expect(updateReady.value).toBe(false);
});

test("applyUpdate asks the waiting worker to skip, and reloads when the controller changes", async () => {
  const s = setup({});
  await registerServiceWorker(s.nav);
  s.registration.waiting = s.worker;
  const reload = vi.fn();
  vi.stubGlobal("location", { reload });
  applyUpdate(s.nav);
  expect(s.worker.postMessage).toHaveBeenCalledWith({ type: "SKIP_WAITING" });
  expect(reload).not.toHaveBeenCalled();
  s.container.fire("controllerchange");
  expect(reload).toHaveBeenCalledTimes(1);
  vi.unstubAllGlobals();
});
