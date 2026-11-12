// The app shell, kept so the app opens without the network. It never touches the API or the live feed: those
// pass straight through, so a stale answer can never stand in for the Studio's.
const VERSION = "__AIRLIE_BUILD__";
const CACHE = `airlie-shell-${VERSION}`;
const SHELL = [
  "/",
  "/offline.html",
  "/manifest.webmanifest",
  "/icon-192.png",
  "/icon-512.png",
  "/icon-maskable-512.png",
];

self.addEventListener("install", (event) => {
  // no skipWaiting here: a new worker waits until the person taps "Reload" (SKIP_WAITING below)
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(names.filter((n) => n.startsWith("airlie-shell-") && n !== CACHE).map((n) => caches.delete(n))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/v1/")) return;
  if ((request.headers.get("accept") || "").includes("text/event-stream")) return;

  if (url.pathname.startsWith("/assets/")) {
    event.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(request).then(
          (hit) =>
            hit ||
            fetch(request).then((response) => {
              if (response.ok) cache.put(request, response.clone());
              return response;
            }),
        ),
      ),
    );
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() =>
        caches.open(CACHE).then((cache) => cache.match("/").then((shell) => shell || cache.match("/offline.html"))),
      ),
    );
  }
});

// A push shows a notification; bad or missing data still shows "airlie" rather than nothing (a silent push can
// get the subscription revoked).
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() || {} : {};
  } catch {
    data = {};
  }
  const title = typeof data.title === "string" && data.title ? data.title : "airlie";
  const options = { icon: "/icon-192.png", badge: "/icon-192.png" };
  if (typeof data.body === "string") options.body = data.body;
  if (typeof data.url === "string") options.data = { url: data.url };
  event.waitUntil(self.registration.showNotification(title, options));
});

// A tap opens the page it is about: an open window is focused and sent there, else a new one opens.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      const open = windows[0];
      if (!open) return self.clients.openWindow(url);
      return open.focus().then(() => open.navigate(url));
    }),
  );
});
