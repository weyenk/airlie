// The shell: a hash router over the screens, the key guard, and (Task 4) the header, theme and navigation.
import { getKey, setAuthHandler } from "./api.js";
import { clear, h } from "./dom.js";
import * as signin from "./screens/signin.js";

const ROUTES = [
  { re: /^#\/?$/, nav: "chat", load: () => import("./screens/chat.js") },
  { re: /^#\/jobs\/?$/, nav: "jobs", load: () => import("./screens/jobs.js") },
  { re: /^#\/jobs\/([^/]+)$/, nav: "jobs", load: () => import("./screens/job.js") },
  { re: /^#\/board(?:\/([^/]+))?$/, nav: "board", load: () => import("./screens/board.js") },
  { re: /^#\/(topology|metrics|admin)$/, nav: null, load: () => import("./screens/soon.js") },
  { re: /^#\/signin$/, nav: null, load: async () => signin },
];

const app = document.getElementById("app");
let cleanup = null;
let current = "";
const ctx = {
  returnTo: "",
  go(hash) {
    if (location.hash === hash) route();
    else location.hash = hash;
  },
};

setAuthHandler(() => {
  if (!location.hash.startsWith("#/signin")) {
    ctx.returnTo = location.hash || "#/";
    ctx.go("#/signin");
  }
});

async function route() {
  const hash = location.hash || "#/";
  const found = ROUTES.map((r) => ({ r, m: hash.match(r.re) })).find((x) => x.m);
  if (!found) return ctx.go("#/");
  if (!getKey() && found.r.load !== ROUTES.at(-1).load) {
    ctx.returnTo = hash;
    return ctx.go("#/signin");
  }
  const token = (current = hash + Math.random());
  let screen;
  try {
    screen = await found.r.load();
  } catch (e) {
    clear(app).append(h("p", { class: "error", style: { padding: "20px" } }, `This screen failed to load: ${e.message}`));
    return;
  }
  if (token !== current) return; // the hash moved on while the module loaded
  if (cleanup) {
    try {
      cleanup();
    } catch {
      /* a screen's cleanup must not stop the next one */
    }
    cleanup = null;
  }
  document.title = screen.title ? `${screen.title} · airlie` : "airlie";
  clear(app);
  const params = found.m.slice(1);
  const out = screen.render(app, params, ctx);
  cleanup = typeof out === "function" ? out : null;
}

window.addEventListener("hashchange", route);
route();
