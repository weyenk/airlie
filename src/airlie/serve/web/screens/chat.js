// Chat: the thread (kept in sessionStorage), a streamed prose reply, the status strip, the composer with the
// repository picker, the + that opens Start new work, and the / menu of chat skills (/story first).
import { request, stream } from "../api.js";
import { jobCard, storyCard } from "../cards.js";
import { append, chip, clear, h, icon } from "../dom.js";
import { markdown } from "../md.js";

export const title = "Chat";

const THREAD = "airlie.chat";
const DRAFT = "airlie.chat.draft";
const REPO = "airlie.repo";
const HISTORY = 20; // turns sent back to the model

const WELCOME =
  "Hi, I'm airlie's assistant. Ask me how airlie works or what's going on. I can't build anything from here: type / for commands such as /story, or use Start new work.";
const QUICK = ["What can you do?", "What's running right now?", "Start new work"];

// ---- storage ----
function load(key, fallback) {
  try {
    const v = sessionStorage.getItem(key);
    return v === null ? fallback : JSON.parse(v);
  } catch {
    return fallback;
  }
}
function save(key, value) {
  try {
    sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* this page only */
  }
}
export function rememberedRepo() {
  try {
    return localStorage.getItem(REPO) || "";
  } catch {
    return "";
  }
}

// ---- screen ----
export function render(root, _params, ctx) {
  const thread = load(THREAD, []);
  let busy = null; // the AbortController of the reply being streamed
  let timer = null;

  const strip = h("div", { class: "row strip" });
  const list = h("div", { class: "thread", "aria-label": "Conversation" });
  const scroller = h("div", { class: "thread-scroll" }, list);
  const input = h("textarea", {
    id: "msg",
    rows: 1,
    placeholder: "Ask airlie, or type / for commands…",
    "aria-label": "Message",
    value: load(DRAFT, ""),
  });
  const send = h("button", { type: "submit", class: "round primary", "aria-label": "Send" }, icon("send", 20));
  const plus = h("button", { type: "button", class: "round", "aria-label": "Start new work", title: "Start new work", onclick: () => ctx.go("#/start") }, icon("plus", 20));
  const repoPick = h("select", { class: "repo-pick mono", "aria-label": "Repository" });
  repoPick.addEventListener("change", () => {
    try {
      localStorage.setItem(REPO, repoPick.value);
    } catch {
      /* this page only */
    }
  });
  const menu = h("div", { class: "slash-menu", role: "listbox", "aria-label": "Commands", hidden: true });
  const composer = h(
    "form",
    { class: "composer", "aria-label": "Message airlie", onsubmit: onSend },
    menu,
    h("div", { class: "composer-row" }, plus, input, send),
    h("div", { class: "row small muted composer-meta" }, h("span", null, "repository"), repoPick, h("a", { href: "#/start" }, "Start new work")),
  );

  root.append(h("div", { class: "chat" }, strip, scroller, composer));

  // -- thread rendering --
  function bubbleFor(item) {
    const body = h("div", { class: "them" });
    fill(body, item);
    return body;
  }

  function fill(body, item) {
    clear(body);
    if (item.started) {
      body.append(jobCard(item.started));
    } else if (item.action === "story" && !item.streaming && item.text && !item.error && !item.cut) {
      body.append(
        storyCard(item.storyText ?? item.text, {
          repo: () => repoPick.value || rememberedRepo(),
          onEdit: (t) => {
            item.storyText = t;
            save(THREAD, withoutEls(thread));
          },
          onStart: async ({ text, repo }) => {
            const out = await request("/v1/airlie/stories", { method: "POST", body: { text, repo } });
            item.started = { job_id: out.job_id, kind: out.kind, label: `${shortRepo(repo)} · story` };
            save(THREAD, withoutEls(thread));
            fill(body, item);
          },
        }),
      );
    } else if (item.text) {
      body.append(markdown(item.text));
    } else if (item.streaming) {
      body.append(h("span", { class: "muted" }, item.thinking ? "Thinking…" : "Writing…"));
    }
    if (item.cut) body.append(h("div", { class: "error" }, "airlie stopped answering"));
    if (item.error) body.append(h("div", { class: "error" }, item.error));
  }

  function renderItem(item) {
    if (item.role === "user") return h("div", { class: "me" }, item.text);
    if (item.role === "card") return jobCard(item);
    const bubble = bubbleFor(item);
    item.el = bubble;
    return h("div", { class: "them-row" }, h("span", { class: "orb", "aria-hidden": "true" }), bubble);
  }

  function renderThread() {
    clear(list);
    if (!thread.length) {
      list.append(
        h(
          "div",
          { class: "them-row" },
          h("span", { class: "orb", "aria-hidden": "true" }),
          h(
            "div",
            { class: "stack", style: { gap: "8px", minWidth: 0 } },
            h("div", { class: "them" }, WELCOME),
            h("div", { class: "row" }, QUICK.map((q) => h("button", { type: "button", class: "quick", onclick: () => quick(q) }, q))),
          ),
        ),
      );
    }
    for (const item of thread) list.append(renderItem(item));
    scrollDown();
  }

  function push(item) {
    thread.push(item);
    list.append(renderItem(item));
    save(THREAD, thread);
    scrollDown();
  }

  function scrollDown() {
    scroller.scrollTop = scroller.scrollHeight;
  }

  function shortRepo(path) {
    return String(path).split("/").filter(Boolean).slice(-1)[0] || path;
  }

  function quick(text) {
    if (text === "Start new work") return ctx.go("#/start");
    input.value = text;
    onSend(new Event("submit"));
  }

  // -- sending --
  async function onSend(ev) {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text || busy) return;
    const skill = skillOf(text);
    const sent = skill ? skill.rest : text;
    if (!sent) {
      input.focus(); // "/story" alone: say what for
      return;
    }
    input.value = "";
    save(DRAFT, "");
    closeMenu();
    autosize();
    const mine = { role: "user", text, sent };
    push(mine);
    const reply = { role: "chat", text: "", streaming: true, thinking: false, action: skill?.action || "" };
    push(reply);
    const messages = thread
      .filter((m) => m.role === "user" || (m.role === "chat" && m.text))
      .slice(-HISTORY)
      .map((m) => ({ role: m.role === "user" ? "user" : "assistant", content: m.sent ?? m.text }));
    busy = new AbortController();
    send.disabled = true;
    let lastPaint = 0;
    const repaint = (force) => {
      const now = performance.now();
      if (!force && now - lastPaint < 80) return;
      lastPaint = now;
      fill(reply.el, reply);
      scrollDown();
    };
    try {
      await stream(
        "/v1/chat/completions",
        { model: "chat", messages, stream: true, ...(skill ? { skill: skill.name } : {}) },
        (delta) => {
          reply.text += delta;
          repaint(false);
        },
        {
          signal: busy.signal,
          onReasoning: () => {
            if (!reply.thinking) {
              reply.thinking = true;
              repaint(true);
            }
          },
        },
      );
    } catch (e) {
      if (e?.name === "AuthError") {
        // keep what was typed: it goes back into the composer after sign-in
        thread.splice(thread.indexOf(mine), 2);
        save(THREAD, thread);
        save(DRAFT, text);
        return;
      }
      if (e?.name === "StreamCut") reply.cut = true;
      else if (e?.name !== "AbortError") reply.error = `Can't reach airlie: ${e.message}`;
    } finally {
      reply.streaming = false;
      if (reply.el) fill(reply.el, reply);
      save(THREAD, withoutEls(thread));
      busy = null;
      send.disabled = false;
      scrollDown();
      refreshStrip();
    }
  }

  function withoutEls(items) {
    return items.map(({ el, ...rest }) => rest);
  }

  // -- the / menu --
  let skills = [];
  let shown = [];
  let active = 0;

  /** "/name rest" for a known skill; null when the text is not a skill command. */
  function skillOf(text) {
    const m = text.match(/^\/(\S+)(?:\s+([\s\S]*))?$/);
    const found = m && skills.find((s) => s.name === m[1]);
    return found ? { name: found.name, action: found.action, rest: (m[2] ?? "").trim() } : null;
  }

  function closeMenu() {
    shown = [];
    menu.hidden = true;
  }

  function paintMenu() {
    append(
      clear(menu),
      shown.map((s, i) =>
        h(
          "button",
          { type: "button", role: "option", class: "slash-item", "aria-selected": i === active ? "true" : "false", onclick: () => choose(s) },
          h("strong", null, `/${s.name}`),
          h("span", { class: "small muted" }, s.summary),
        ),
      ),
    );
    menu.hidden = !shown.length;
  }

  function updateMenu() {
    const m = input.value.match(/^\/(\S*)$/); // only while the first word is being typed
    shown = m ? skills.filter((s) => s.name.startsWith(m[1].toLowerCase())) : [];
    active = Math.min(active, Math.max(shown.length - 1, 0));
    paintMenu();
  }

  function choose(s) {
    input.value = `/${s.name} `;
    closeMenu();
    input.focus();
    save(DRAFT, input.value);
    autosize();
  }

  async function loadSkills() {
    try {
      ({ skills } = await request("/v1/airlie/chat/skills"));
    } catch {
      skills = []; // no menu; "/x" is then sent as it was typed
    }
    updateMenu();
  }

  // -- composer behaviour --
  function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  }
  input.addEventListener("input", () => {
    save(DRAFT, input.value);
    autosize();
    updateMenu();
  });
  input.addEventListener("keydown", (ev) => {
    if (shown.length) {
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        active = (active + (ev.key === "ArrowDown" ? 1 : shown.length - 1)) % shown.length;
        paintMenu();
        return;
      }
      if (ev.key === "Enter" || ev.key === "Tab") {
        ev.preventDefault();
        choose(shown[active]);
        return;
      }
      if (ev.key === "Escape") {
        ev.preventDefault();
        closeMenu();
        return;
      }
    }
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault();
      onSend(ev);
    }
  });

  // -- status strip and repositories --
  async function refreshStrip() {
    try {
      const [{ jobs }, { stories }] = await Promise.all([request("/v1/airlie/jobs"), request("/v1/airlie/stories")]);
      const running = jobs.filter((j) => j.state === "running").length;
      const failed = jobs.filter((j) => j.state === "failed").length + stories.filter((s) => s.blocked).length;
      append(clear(strip), [
        failed ? h("a", { href: "#/jobs", class: "chip warn" }, `${failed} need${failed === 1 ? "s" : ""} you`) : null,
        h("a", { href: "#/jobs", class: `chip ${running ? "run" : "mute"}` }, `${running} running`),
        chip("ok", "Studio online"),
      ]);
    } catch (e) {
      if (e?.name !== "AuthError") append(clear(strip), [chip("err", "Studio unreachable")]);
    }
  }

  async function loadRepos() {
    try {
      const { repos } = await request("/v1/airlie/repos");
      const remembered = rememberedRepo();
      append(clear(repoPick), [
        repos.length ? null : h("option", { value: "" }, "no repositories"),
        repos.map((r) => h("option", { value: r, selected: r === remembered || null }, shortRepo(r))),
      ]);
      if (!remembered && repos.length) repoPick.value = repos[0];
    } catch {
      append(clear(repoPick), [h("option", { value: "" }, "repositories unavailable")]);
    }
  }

  renderThread();
  autosize();
  refreshStrip();
  loadRepos();
  loadSkills();
  timer = setInterval(refreshStrip, 15000);
  if (matchMedia("(min-width: 768px)").matches) input.focus();

  return () => {
    clearInterval(timer);
    busy?.abort();
  };
}
