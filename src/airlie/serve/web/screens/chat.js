// Chat with the chief: the thread (kept in sessionStorage), a streamed reply, the status strip, the composer
// with the repository picker and the + that opens Start new work.
import { request, stream } from "../api.js";
import { jobCard, planCard, startRun } from "../cards.js";
import { append, chip, clear, h, icon } from "../dom.js";
import { markdown } from "../md.js";

export const title = "Chat";

const THREAD = "airlie.chat";
const DRAFT = "airlie.chat.draft";
const REPO = "airlie.repo";
const HISTORY = 20; // turns sent back to the chief

const WELCOME =
  "Hi, I'm airlie's chief. Ask me what's going on, or describe new work: I'll plan it, and nothing is built until you approve the plan.";
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

// ---- the chief's JSON ----
/** The chief answers with one JSON object. Parsed when whole; while it streams, the `answer` string is
 * pulled out of the partial text so the person sees it grow. */
export function unwrap(text) {
  const t = String(text ?? "").trim();
  const start = t.indexOf("{");
  if (start < 0) return { kind: "text", text: t, partial: false };
  try {
    const obj = JSON.parse(t.slice(start, t.lastIndexOf("}") + 1));
    if (obj && typeof obj === "object") {
      if (obj.type === "plan" && Array.isArray(obj.subtasks)) return { kind: "plan", plan: obj, partial: false };
      const answer = obj.answer ?? obj.synthesis ?? null;
      if (typeof answer === "string") return { kind: "text", text: answer, open: obj.open_questions, partial: false };
      return { kind: "text", text: JSON.stringify(obj, null, 2), partial: false };
    }
  } catch {
    /* not whole yet, or not JSON */
  }
  const m = t.match(/"answer"\s*:\s*"/);
  if (!m) return { kind: "text", text: /^\{\s*"type"\s*:\s*"plan"/.test(t) ? "" : t, partial: true };
  let s = t.slice(m.index + m[0].length);
  for (let n = 0; n < 6 && s.length; n += 1) {
    try {
      return { kind: "text", text: JSON.parse(`"${s}"`), partial: true };
    } catch {
      s = s.replace(/\\?.$/, ""); // a trailing partial escape, or the closing quote and what follows
      const end = s.lastIndexOf('"');
      if (n === 0 && end >= 0 && /"\s*[,}]?\s*$/.test(t)) s = s.slice(0, end);
    }
  }
  return { kind: "text", text: "", partial: true };
}

// ---- screen ----
export function render(root, _params, ctx) {
  const thread = load(THREAD, []);
  let busy = null; // the AbortController of the reply being streamed
  let timer = null;

  const strip = h("div", { class: "row strip" });
  const list = h("div", { class: "thread", "aria-label": "Conversation with the chief" });
  const scroller = h("div", { class: "thread-scroll" }, list);
  const input = h("textarea", {
    id: "msg",
    rows: 1,
    placeholder: "Ask airlie, or describe new work…",
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
  const composer = h(
    "form",
    { class: "composer", "aria-label": "Message the chief", onsubmit: onSend },
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
    const u = item.reply ?? unwrap(item.text);
    if (u.kind === "plan") {
      body.append(
        planCard(u.plan, {
          approved: item.approved,
          onApprove: async (plan) => {
            const repo = repoPick.value || rememberedRepo();
            if (!repo) throw new Error("Pick a repository first (under the message box).");
            const goal = item.goal || lastUserText(item) || "the plan above";
            const out = await startRun({ goal, repo, plan });
            item.approved = true;
            push({ role: "card", job_id: out.job_id, kind: out.kind, label: `${shortRepo(repo)} · studio run` });
            save(THREAD, thread);
          },
          onChange: () => {
            input.value = "Change the plan: ";
            input.focus();
          },
        }),
      );
    } else {
      if (u.text) body.append(markdown(u.text));
      else if (item.streaming) body.append(h("span", { class: "muted" }, item.thinking ? "Thinking…" : "Writing…"));
      if (u.open?.length) body.append(h("ul", { class: "small muted" }, u.open.map((q) => h("li", null, q))));
    }
    if (item.cut) body.append(h("div", { class: "error" }, "airlie stopped answering"));
    if (item.error) body.append(h("div", { class: "error" }, item.error));
  }

  function lastUserText(item) {
    const i = thread.indexOf(item);
    for (let j = i - 1; j >= 0; j -= 1) if (thread[j].role === "user") return thread[j].text;
    return "";
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
    input.value = "";
    save(DRAFT, "");
    autosize();
    const mine = { role: "user", text };
    push(mine);
    const reply = { role: "chief", text: "", streaming: true, thinking: false, goal: text };
    push(reply);
    const messages = thread
      .filter((m) => m.role === "user" || (m.role === "chief" && m.text))
      .slice(-HISTORY)
      .map((m) => ({ role: m.role === "user" ? "user" : "assistant", content: m.text }));
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
        { model: "chief", messages, stream: true },
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
      reply.reply = reply.text ? unwrap(reply.text) : null;
      delete reply.el?.dataset.x;
      if (reply.el) fill(reply.el, reply);
      save(THREAD, strip_els(thread));
      busy = null;
      send.disabled = false;
      scrollDown();
      refreshStrip();
    }
  }

  function strip_els(items) {
    return items.map(({ el, ...rest }) => rest);
  }

  // -- composer behaviour --
  function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  }
  input.addEventListener("input", () => {
    save(DRAFT, input.value);
    autosize();
  });
  input.addEventListener("keydown", (ev) => {
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
  timer = setInterval(refreshStrip, 15000);
  if (matchMedia("(min-width: 768px)").matches) input.focus();

  return () => {
    clearInterval(timer);
    busy?.abort();
  };
}
