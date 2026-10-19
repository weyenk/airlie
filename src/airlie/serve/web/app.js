// The shell: header, navigation (the top nav on desktop, the tab bar on phones), the theme toggle, and a hash
// router over the screens. A screen module exports `title`, `render(root, params, ctx)` (returning a cleanup
// function or nothing) and, for Sign in, `bare` (no chrome around it).
import { getKey, setAuthHandler, signOut as endSession, whoami } from "./api.js";
import { clear, h, icon } from "./dom.js";
import * as signin from "./screens/signin.js";

const ROUTES = [
  { re: /^#\/?$/, nav: "chat", load: () => import("./screens/chat.js") },
  { re: /^#\/jobs\/?$/, nav: "jobs", load: () => import("./screens/jobs.js") },
  { re: /^#\/jobs\/([^/]+)$/, nav: "jobs", load: () => import("./screens/job.js") },
  { re: /^#\/start$/, nav: "chat", load: () => import("./screens/start.js") },
  { re: /^#\/board(?:\/([^/]+))?$/, nav: "board", load: () => import("./screens/board.js") },
  { re: /^#\/topology$/, nav: "topology", load: () => import("./screens/soon.js") },
  { re: /^#\/metrics$/, nav: "metrics", load: () => import("./screens/metrics.js") },
  { re: /^#\/admin$/, nav: "admin", load: () => import("./screens/admin.js") },
  { re: /^#\/join\/([^/]+)$/, nav: null, public: true, load: () => import("./screens/join.js") },
  { re: /^#\/signin$/, nav: null, public: true, load: async () => signin },
];

const NAV = [
  ["chat", "#/", "Chat"],
  ["jobs", "#/jobs", "Jobs"],
  ["board", "#/board", "Board"],
  ["topology", "#/topology", "Topology"],
  ["metrics", "#/metrics", "Metrics"],
  ["admin", "#/admin", "Admin"],
];

// ---- theme ----
const THEME = "airlie.theme";

function theme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function setTheme(next) {
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem(THEME, next);
  } catch {
    /* this page only */
  }
  themeButton.replaceChildren(icon(next === "dark" ? "sun" : "moon", 18));
  themeButton.setAttribute("aria-label", next === "dark" ? "Switch to light theme" : "Switch to dark theme");
}

const themeButton = h("button", { type: "button", class: "iconbtn", onclick: () => setTheme(theme() === "dark" ? "light" : "dark") });

// ---- shell ----
const nav = h("nav", { class: "nav", "aria-label": "Main" }, NAV.map(([id, href, label]) => h("a", { href, dataset: { nav: id } }, label)));
const tabs = h(
  "nav",
  { class: "tabs", "aria-label": "Main" },
  NAV.map(([id, href, label]) => h("a", { href, dataset: { nav: id } }, icon(id), h("span", null, label))),
);
const signOut = h(
  "button",
  {
    type: "button",
    class: "linkbtn",
    onclick: async () => {
      await endSession();
      ctx.signedIn = false;
      ctx.returnTo = "";
      ctx.go("#/signin");
    },
  },
  "Sign out",
);
const main = h("main", { id: "screen", class: "main" });
const shell = h(
  "div",
  { class: "shell" },
  h(
    "header",
    { class: "top" },
    h(
      "div",
      { class: "wrap bar" },
      h("a", { href: "#/", class: "brand" }, h("span", { class: "orb", "aria-hidden": "true" }), "airlie"),
      nav,
      h("div", { class: "right" }, themeButton, signOut),
    ),
  ),
  main,
  tabs,
);
setTheme(theme());
document.getElementById("app").replaceWith(shell);

function markCurrent(id) {
  for (const a of [...nav.children, ...tabs.children]) {
    if (a.dataset.nav === id) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
}

// ---- router ----
let cleanup = null;
let current = "";
const ctx = {
  returnTo: "",
  signedIn: false, // the server knew us (Tailscale identity or a passkey session) the last time we asked
  go(hash) {
    if (location.hash === hash) route();
    else location.hash = hash;
  },
};

setAuthHandler(() => {
  ctx.signedIn = false;
  if (!location.hash.startsWith("#/signin") && !location.hash.startsWith("#/join/")) {
    ctx.returnTo = location.hash || "#/";
    ctx.go("#/signin");
  }
});

/** Without a stored key, ask the server once whether it already knows us (behind tailscale serve it does). */
async function knownToServer() {
  if (ctx.signedIn) return true;
  try {
    ctx.signedIn = Boolean(await whoami());
  } catch {
    ctx.signedIn = false; // unreachable: Sign in says so
  }
  return ctx.signedIn;
}

async function route() {
  const hash = location.hash || "#/";
  const found = ROUTES.map((r) => ({ r, m: hash.match(r.re) })).find((x) => x.m);
  if (!found) return ctx.go("#/");
  const token = (current = hash + Math.random());
  if (!found.r.public && !getKey() && !(await knownToServer())) {
    if (token !== current) return;
    ctx.returnTo = hash;
    return ctx.go("#/signin");
  }
  if (token !== current) return; // the hash moved on while we asked the server
  let screen;
  try {
    screen = await found.r.load();
  } catch (e) {
    clear(main).append(h("p", { class: "error pad" }, `This screen failed to load: ${e.message}`));
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
  shell.classList.toggle("bare", Boolean(screen.bare));
  const params = found.m.slice(1);
  markCurrent(found.r.nav === "$1" ? params[0] : found.r.nav);
  clear(main);
  main.scrollTop = 0;
  const out = screen.render(main, params, ctx);
  cleanup = typeof out === "function" ? out : null;
}

window.addEventListener("hashchange", route);
route();
