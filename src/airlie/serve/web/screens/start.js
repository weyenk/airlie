// Start new work: a written request or a board story, in a repository, as a story (layers → draft PRs) or a
// studio run (the chief plans first; the plan is approved on the job's screen).
import { request } from "../api.js";
import { append, clear, h } from "../dom.js";
import { rememberedRepo } from "./chat.js";

export const title = "Start new work";

function shortRepo(path) {
  return String(path).split("/").filter(Boolean).slice(-1)[0] || path;
}

/** Add a job card to the chat thread so the conversation shows what was started. */
export function noteJobInChat(card) {
  try {
    const thread = JSON.parse(sessionStorage.getItem("airlie.chat") || "[]");
    thread.push({ role: "card", ...card });
    sessionStorage.setItem("airlie.chat", JSON.stringify(thread));
  } catch {
    /* the chat is a convenience here */
  }
}

export function render(root, _params, ctx) {
  let source = "text"; // text | story
  const repo = h("select", { class: "field", id: "repo" });
  const how = h(
    "select",
    { class: "field", id: "how" },
    h("option", { value: "story" }, "Story: layers → draft PRs"),
    h("option", { value: "studio" }, "Studio run: one branch"),
  );
  const text = h("textarea", { class: "field", id: "what", rows: 5, placeholder: "What should airlie build?" });
  const card = h("input", { class: "field mono", id: "card", placeholder: "HDS-5 or AL-12", autocapitalize: "characters" });
  const textLabel = h("label", { class: "block", for: "what" }, "What should airlie build?", text);
  const cardLabel = h("label", { class: "block", for: "card" }, "Board story", card);
  const repoLabel = h("label", { class: "block", for: "repo" }, "Repository", repo);
  const error = h("span", { class: "error" });
  const start = h("button", { type: "submit", class: "btn primary" }, "Start");
  const note = h("span", { class: "muted small" }, "The chief plans first. You approve the plan before anything is built, and nothing merges without your review.");

  const textBtn = h("button", { type: "button", class: "btn", "aria-pressed": "true" }, "Written request");
  const storyBtn = h("button", { type: "button", class: "btn", "aria-pressed": "false" }, "Board story");
  function pick(which) {
    source = which;
    textBtn.setAttribute("aria-pressed", String(which === "text"));
    storyBtn.setAttribute("aria-pressed", String(which === "story"));
    textLabel.hidden = which !== "text";
    cardLabel.hidden = which !== "story";
    // an issue's repository is its repo: label; only its text can pick one
    repoLabel.hidden = which === "story";
    how.disabled = which === "story";
    if (which === "story") how.value = "story";
    updateNote();
  }
  textBtn.addEventListener("click", () => pick("text"));
  storyBtn.addEventListener("click", () => pick("story"));
  how.addEventListener("change", updateNote);
  function updateNote() {
    note.textContent =
      how.value === "story"
        ? "Product writes the story, the architect its layers, and each layer is built test-first on its own branch; the result is stacked draft PRs for your review."
        : "The chief plans first. You approve the plan before anything is built, and nothing merges without your review.";
  }

  async function submit(ev) {
    ev.preventDefault();
    error.textContent = "";
    start.disabled = true;
    try {
      let out;
      let label;
      if (source === "story") {
        const key = card.value.trim().toUpperCase();
        if (!key) throw new Error("Which story? Give its key.");
        out = await request("/v1/airlie/stories", { method: "POST", body: { card: key } });
        label = `${key} · story`;
      } else {
        const goal = text.value.trim();
        if (!goal) throw new Error("Say what airlie should build.");
        if (!repo.value) throw new Error("Pick a repository.");
        if (how.value === "story") {
          out = await request("/v1/airlie/stories", { method: "POST", body: { text: goal, repo: repo.value } });
          label = `${shortRepo(repo.value)} · story`;
        } else {
          out = await request("/v1/airlie/plans", { method: "POST", body: { goal, repo: repo.value } });
          label = `${shortRepo(repo.value)} · studio plan`;
        }
        try {
          localStorage.setItem("airlie.repo", repo.value);
        } catch {
          /* this page only */
        }
      }
      noteJobInChat({ job_id: out.job_id, kind: out.kind, label });
      ctx.go(`#/jobs/${encodeURIComponent(out.job_id)}`);
    } catch (e) {
      if (e?.name === "AuthError") return;
      error.textContent = e.message;
      start.disabled = false;
    }
  }

  root.append(
    h(
      "div",
      { class: "page" },
      h(
        "form",
        { class: "card pad stack", style: { gap: "18px" }, onsubmit: submit },
        h("div", { class: "row spread" }, h("h1", { class: "h1" }, "Start new work"), h("a", { href: "#/", class: "small" }, "Back to chat")),
        h("div", { role: "group", "aria-label": "What to start from", class: "seg-group" }, textBtn, storyBtn),
        h("div", { class: "grid2" }, repoLabel, h("label", { class: "block", for: "how" }, "How it runs", how)),
        textLabel,
        cardLabel,
        h("div", { class: "row", style: { gap: "12px 16px" } }, start, note),
        error,
      ),
    ),
  );
  pick("text");

  (async () => {
    try {
      const { repos } = await request("/v1/airlie/repos");
      const remembered = rememberedRepo();
      append(clear(repo), [
        repos.length ? null : h("option", { value: "" }, "no repositories under story.repos_root"),
        repos.map((r) => h("option", { value: r, selected: r === remembered || null }, shortRepo(r))),
      ]);
    } catch (e) {
      if (e?.name !== "AuthError") error.textContent = `Can't list repositories: ${e.message}`;
    }
  })();
  text.focus();
}
