// Chat: the thread (kept in sessionStorage), a streamed prose reply, the status strip, the composer with the
// + that attaches pictures, video and files (design 2026-10-22-chat-attachments), and the / menu of chat skills
// (/story starts work, /clear a fresh session). The thread keeps an attachment's id and name, never its bytes.
import { request, stream, upload, uploadUrl } from "../api.js";
import { jobCard, storyCard } from "../cards.js";
import { append, chip, clear, h, icon } from "../dom.js";
import { markdown } from "../md.js";

export const title = "Chat";

const THREAD = "airlie.chat";
const DRAFT = "airlie.chat.draft";
const HISTORY = 20; // turns sent back to the model

const WELCOME =
  "Hi, I'm airlie's assistant. Ask me how airlie works or what's going on. I can't build anything from here: type / for commands such as /story or /clear, or use Start new work.";
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
export { rememberedRepo } from "../cards.js";

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
  const picker = h("input", { type: "file", multiple: true, hidden: true, "aria-label": "Files to attach" });
  const plus = h("button", { type: "button", class: "round", "aria-label": "Attach a file", title: "Attach a picture, video or file", onclick: () => picker.click() }, icon("plus", 20));
  const tray = h("div", { class: "attach-tray", role: "list", "aria-label": "Attachments", hidden: true });
  const menu = h("div", { class: "slash-menu", role: "listbox", "aria-label": "Commands", hidden: true });
  const composer = h(
    "form",
    { class: "composer", "aria-label": "Message airlie", onsubmit: onSend },
    menu,
    tray,
    h("div", { class: "composer-row" }, plus, input, send),
    picker,
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
          repos: () => repos,
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
    if (item.role === "user") return h("div", { class: "me" }, item.attachments?.length ? sentFiles(item.attachments) : null, item.text);
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

  /** /clear: a fresh session. The thread, the draft and any attachment not yet sent go; a reply still
   * streaming is stopped. Stored files are left to expire (chat.attachments.keep_days). */
  function clearThread() {
    busy?.abort();
    for (const p of pending.splice(0)) p.abort?.abort();
    paintTray();
    for (const u of urls.values()) URL.revokeObjectURL(u);
    urls.clear();
    thread.length = 0;
    save(THREAD, thread);
    input.value = "";
    save(DRAFT, "");
    closeMenu();
    autosize();
    renderThread();
    input.focus();
  }

  // -- sending --
  async function onSend(ev) {
    ev.preventDefault();
    const text = input.value.trim();
    if (busy || pending.some((p) => p.state !== "done")) return; // still uploading, or a chip to remove
    if (!text && !pending.length) return;
    const skill = skillOf(text);
    if (skill?.action === "clear") return clearThread();
    const sent = skill ? skill.rest : text;
    if (!sent && !pending.length) {
      input.focus(); // "/story" alone: say what for
      return;
    }
    const attachments = pending.splice(0).map((p) => p.meta);
    paintTray();
    input.value = "";
    save(DRAFT, "");
    closeMenu();
    autosize();
    const mine = { role: "user", text, sent, ...(attachments.length ? { attachments } : {}) };
    push(mine);
    const reply = { role: "chat", text: "", streaming: true, thinking: false, action: skill?.action || "" };
    push(reply);
    const messages = thread
      .filter((m) => m.role === "user" || (m.role === "chat" && m.text))
      .slice(-HISTORY)
      .map((m) => ({ role: m.role === "user" ? "user" : "assistant", content: contentOf(m) }));
    busy = new AbortController();
    updateSend();
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
        pending.push(...attachments.map((meta) => ({ state: "done", meta, name: meta.name, size: meta.size, kind: meta.kind })));
        paintTray();
        return;
      }
      if (e?.name === "StreamCut") reply.cut = true;
      else if (e?.name !== "AbortError") reply.error = `Can't reach airlie: ${e.message}`;
    } finally {
      reply.streaming = false;
      if (reply.el) fill(reply.el, reply);
      save(THREAD, withoutEls(thread));
      busy = null;
      updateSend();
      scrollDown();
      refreshStrip();
    }
  }

  /** A turn as the chat route takes it: text, or text and an `airlie_upload` part per attachment. */
  function contentOf(m) {
    if (m.role !== "user" || !m.attachments?.length) return m.sent ?? m.text;
    const parts = m.sent ? [{ type: "text", text: m.sent }] : [];
    for (const a of m.attachments) parts.push({ type: "airlie_upload", upload: a.id, name: a.name });
    return parts;
  }

  // -- attachments --
  const pending = []; // {state: "uploading"|"done"|"error", name, size, kind, progress, meta, error, abort, url}
  const urls = new Map(); // upload id -> object URL of a picture or video shown in the thread
  let limits = { max_per_message: 10, max_image_mb: 20, max_video_mb: 100, max_file_mb: 25 };

  /** The kind the server will most likely decide, from the browser's type: for the size check before
   * uploading. The server reads the bytes and has the last word. */
  function guessKind(file) {
    if (/^image\/(png|jpeg|gif|webp)$/.test(file.type)) return "image";
    if (/^video\//.test(file.type)) return "video";
    return "file";
  }

  function limitMb(kind) {
    return kind === "image" ? limits.max_image_mb : kind === "video" ? limits.max_video_mb : limits.max_file_mb;
  }

  function sizeText(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }

  function addFiles(files) {
    for (const file of files) {
      const kind = guessKind(file);
      const p = { state: "uploading", name: file.name || "file", size: file.size, kind, progress: 0 };
      pending.push(p);
      if (kind === "image" || kind === "video") p.url = URL.createObjectURL(file);
      if (pending.length > limits.max_per_message) {
        Object.assign(p, { state: "error", error: `up to ${limits.max_per_message} attachments a message` });
        continue;
      }
      if (file.size > limitMb(kind) * 1024 * 1024) {
        const what = kind === "image" ? "a picture" : kind === "video" ? "a video" : "a file";
        Object.assign(p, { state: "error", error: `too large: ${what} may be up to ${limitMb(kind)} MB` });
        continue;
      }
      if (!file.size) {
        Object.assign(p, { state: "error", error: "the file is empty" });
        continue;
      }
      p.abort = new AbortController();
      upload(file, {
        signal: p.abort.signal,
        onProgress: (f) => {
          p.progress = f;
          paintTray();
        },
      })
        .then((meta) => {
          Object.assign(p, { state: "done", meta, kind: meta.kind });
          if (p.url && (meta.kind === "image" || meta.kind === "video")) urls.set(meta.id, p.url);
          else if (p.url) URL.revokeObjectURL(p.url);
        })
        .catch((e) => {
          if (e?.name === "AbortError") return;
          Object.assign(p, { state: "error", error: e?.name === "AuthError" ? "sign in again, then attach it" : e.message });
        })
        .finally(paintTray);
    }
    paintTray();
  }

  function remove(p) {
    p.abort?.abort();
    if (p.url && !(p.meta && urls.get(p.meta.id) === p.url)) URL.revokeObjectURL(p.url);
    pending.splice(pending.indexOf(p), 1);
    paintTray();
  }

  function fileIcon(kind) {
    return h("span", { class: "attach-icon", "aria-hidden": "true" }, icon(kind === "video" ? "play" : "file", 18));
  }

  function paintTray() {
    append(
      clear(tray),
      pending.map((p) =>
        h(
          "div",
          { class: `attach-chip ${p.state}`, role: "listitem", "aria-label": p.name },
          p.kind === "image" && p.url ? h("img", { src: p.url, alt: "" }) : fileIcon(p.kind),
          h(
            "span",
            { class: "stack attach-text" },
            h("span", { class: "attach-name" }, p.name),
            p.state === "error"
              ? h("span", { class: "error small" }, p.error)
              : h("span", { class: "small muted" }, p.state === "uploading" ? `uploading ${Math.round(p.progress * 100)}%` : sizeText(p.size)),
            p.state === "uploading" ? h("progress", { max: 1, value: p.progress, "aria-label": `Uploading ${p.name}` }) : null,
          ),
          h("button", { type: "button", class: "attach-remove", "aria-label": `Remove ${p.name}`, onclick: () => remove(p) }, icon("close", 16)),
        ),
      ),
    );
    tray.hidden = !pending.length;
    updateSend();
  }

  function updateSend() {
    send.disabled = Boolean(busy) || pending.some((p) => p.state !== "done");
  }

  /** What I attached, in my bubble: a picture as itself (fetched again after a reload), a video I attached on
   * this page as a player, anything else as its name. */
  function sentFiles(attachments) {
    return h(
      "div",
      { class: "me-files" },
      attachments.map((a) => {
        const url = urls.get(a.id);
        if (a.kind === "image") {
          const img = h("img", { alt: a.name, src: url || null });
          if (!url) {
            uploadUrl(a.id)
              .then((u) => {
                urls.set(a.id, u);
                img.src = u;
              })
              .catch(() => img.replaceWith(h("span", { class: "me-file" }, fileIcon("file"), `${a.name} (no longer available)`)));
          }
          return img;
        }
        if (a.kind === "video" && url) return h("video", { src: url, controls: true, playsinline: true, preload: "metadata", "aria-label": a.name });
        return h("span", { class: "me-file" }, fileIcon(a.kind), `${a.name} · ${sizeText(a.size)}`);
      }),
    );
  }

  picker.addEventListener("change", () => {
    addFiles([...picker.files]);
    picker.value = ""; // the same file can be picked again
  });
  composer.addEventListener("dragover", (ev) => {
    if (![...(ev.dataTransfer?.types ?? [])].includes("Files")) return;
    ev.preventDefault();
    composer.classList.add("dropping");
  });
  composer.addEventListener("dragleave", () => composer.classList.remove("dropping"));
  composer.addEventListener("drop", (ev) => {
    composer.classList.remove("dropping");
    if (!ev.dataTransfer?.files?.length) return;
    ev.preventDefault();
    addFiles([...ev.dataTransfer.files]);
  });
  input.addEventListener("paste", (ev) => {
    const files = [...(ev.clipboardData?.files ?? [])];
    if (!files.length) return;
    ev.preventDefault();
    addFiles(files);
  });

  async function loadLimits() {
    try {
      limits = await request("/v1/airlie/chat/attachments");
    } catch {
      /* the defaults; the server checks again */
    }
  }

  function withoutEls(items) {
    return items.map(({ el, ...rest }) => rest);
  }

  // -- the / menu --
  let skills = [];
  let repos = [];
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
      ({ repos } = await request("/v1/airlie/repos"));
    } catch {
      repos = []; // the story card then says to pick one
    }
  }

  renderThread();
  autosize();
  refreshStrip();
  loadRepos();
  loadSkills();
  loadLimits();
  timer = setInterval(refreshStrip, 15000);
  if (matchMedia("(min-width: 768px)").matches) input.focus();

  return () => {
    clearInterval(timer);
    busy?.abort();
    for (const p of pending) p.abort?.abort();
    for (const u of urls.values()) URL.revokeObjectURL(u);
  };
}
