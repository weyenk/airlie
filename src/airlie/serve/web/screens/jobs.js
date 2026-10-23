// Workflows: the stories (each opens its pipeline), what this airlie is running, what needs you, and recent results.
import { request } from "../api.js";
import { ago, append, chip, clear, h } from "../dom.js";

export const title = "Workflows";

const STATE_CHIP = { running: ["run", "Running"], done: ["ok", "Done"], failed: ["err", "Failed"], cancelled: ["mute", "Stopped"] };

export function stateChip(state) {
  const [kind, label] = STATE_CHIP[state] || ["mute", state];
  return chip(kind, label);
}

function jobLink(id) {
  return `#/jobs/${encodeURIComponent(id)}`;
}

function storyLink(key) {
  return `#/workflows/${encodeURIComponent(key)}`;
}

function shortRepo(path) {
  return path ? String(path).split("/").filter(Boolean).slice(-1)[0] : "";
}

export function render(root) {
  const running = h("div", { class: "rows" });
  const needs = h("div", { class: "rows" });
  const storyRows = h("div", { class: "rows" });
  const recent = h("div", { class: "rows" });
  const summary = h("span", { class: "small muted" });

  root.append(
    h(
      "div",
      { class: "page" },
      h("div", { class: "row spread" }, h("h1", { class: "h1" }, "Workflows"), h("a", { href: "#/start", class: "btn primary small" }, "Start new work")),
      h("section", { class: "card pad stack" }, h("h2", { class: "h2" }, "Stories"), h("p", { class: "small muted", style: { margin: 0 } }, "Pick a story to see its pipeline."), storyRows),
      h("section", { class: "card pad stack" }, h("div", { class: "row spread" }, h("h2", { class: "h2" }, "Running"), summary), running),
      h("section", { class: "card pad stack" }, h("h2", { class: "h2" }, "Needs you"), needs),
      h("section", { class: "card pad stack" }, h("h2", { class: "h2" }, "Recent results"), recent),
    ),
  );

  let timer = null;
  let gone = false;

  async function refresh() {
    let jobs = [];
    let stories = [];
    let runs = [];
    try {
      [{ jobs }, { stories }, { runs }] = await Promise.all([
        request("/v1/airlie/jobs"),
        request("/v1/airlie/stories"),
        request("/v1/airlie/runs?limit=20"),
      ]);
    } catch (e) {
      if (e?.name === "AuthError") return;
      append(clear(running), [h("div", { class: "error" }, `Can't reach airlie: ${e.message}`)]);
      return;
    }
    if (gone) return;
    const goals = new Map(runs.map((r) => [r.id, r]));
    const label = (job) => {
      const run = goals.get(job.id);
      const story = stories.find((s) => s.key === job.id);
      return run?.goal || story?.title || job.id;
    };
    const live = jobs.filter((j) => j.state === "running");
    summary.textContent = `${live.length} job${live.length === 1 ? "" : "s"}`;
    append(clear(running), [
      live.length ? null : h("div", { class: "empty" }, "Nothing is running. Start new work from the chat or the button above."),
      live.map((j) =>
        h(
          "a",
          { href: jobLink(j.id), class: "jobrow" },
          h("div", { class: "stack", style: { gap: "2px", minWidth: 0 } }, h("strong", { class: "ellipsis" }, label(j)), h("span", { class: "mono small muted" }, `${j.kind} · ${j.id}`)),
          h("div", { class: "row" }, chip("run", `${j.kind} · running`), h("span", { class: "mono small muted" }, ago(Date.now() - j.started * 1000))),
        ),
      ),
    ]);
    const failed = jobs.filter((j) => j.state === "failed");
    const blocked = stories.filter((s) => s.blocked);
    append(clear(needs), [
      failed.length || blocked.length ? null : h("div", { class: "empty" }, "Nothing needs you right now."),
      failed.map((j) =>
        h(
          "a",
          { href: jobLink(j.id), class: "jobrow notice" },
          h("div", { class: "stack", style: { gap: "2px", minWidth: 0 } }, h("strong", { class: "ellipsis" }, label(j)), h("span", { class: "small error" }, j.error || "failed")),
          chip("err", "Failed"),
        ),
      ),
      blocked.map((s) =>
        h(
          "a",
          { href: storyLink(s.key), class: "jobrow notice" },
          h("div", { class: "stack", style: { gap: "2px", minWidth: 0 } }, h("strong", null, h("span", { class: "mono" }, s.key), s.title ? ` · ${s.title}` : ""), h("span", { class: "small" }, s.blocked)),
          chip("warn", "Blocked"),
        ),
      ),
    ]);
    append(clear(storyRows), [
      stories.length ? null : h("div", { class: "empty" }, "No stories yet."),
      stories.map((s) => {
        const job = jobs.find((j) => j.id === s.key);
        return h(
          "a",
          { href: storyLink(s.key), class: "jobrow", dataset: { story: s.key } },
          h("div", { class: "stack", style: { gap: "2px", minWidth: 0 } }, h("strong", { class: "ellipsis" }, h("span", { class: "mono" }, s.key), s.title ? ` · ${s.title}` : ""), h("span", { class: "mono small muted" }, [s.stage ? `stage ${s.stage}` : "not started", shortRepo(s.repo)].filter(Boolean).join(" · "))),
          s.blocked ? chip("warn", "Blocked") : job ? stateChip(job.state) : chip("mute", s.stage || "new"),
        );
      }),
    ]);
    append(clear(recent), [
      runs.length ? null : h("div", { class: "empty" }, "No runs yet."),
      runs.map((r) => {
        const job = jobs.find((j) => j.id === r.id);
        const status = job && job.state === "running" ? chip("run", "Running") : r.ok === true ? chip("ok", "Done") : r.ok === false ? chip("err", "Failed") : chip("mute", r.started ? "No result" : "Empty");
        return h(
          job ? "a" : "div",
          { href: job ? jobLink(r.id) : null, class: "jobrow" },
          h("div", { class: "stack", style: { gap: "2px", minWidth: 0 } }, h("span", { class: "ellipsis" }, r.goal || h("span", { class: "muted" }, "(no goal recorded)")), h("span", { class: "mono small muted" }, [r.kind === "plan" ? "plan" : "run", r.id, shortRepo(r.repo), r.wall_ms ? ago(r.wall_ms) : null].filter(Boolean).join(" · "))),
          status,
        );
      }),
    ]);
  }

  refresh();
  timer = setInterval(refresh, 10000);
  return () => {
    gone = true;
    clearInterval(timer);
  };
}
