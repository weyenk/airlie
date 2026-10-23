// One job: its event log followed by long polls, drawn as the agent tree (chief → subtasks → reviewer and
// synthesis) with the selected node's latest steps, the result once it ends, and Stop.
import { request } from "../api.js";
import { planCard, startRun } from "../cards.js";
import { ago, append, chip, clear, clock, h } from "../dom.js";
import { markdown } from "../md.js";
import { stateChip } from "./jobs.js";
import { noteJobInChat } from "./start.js";

export const title = "Job";

const WAIT = 20; // seconds a long poll is held
const STEPS_SHOWN = 6;

/** What the events say so far: the plan's nodes and their state, the story's stage, the run's end. */
export function digest(events) {
  const m = {
    goal: null,
    repo: null,
    mode: "run",
    started: null,
    planned: false,
    planErrors: [],
    nodes: new Map(),
    reviewed: false,
    synthesis: null,
    done: null,
    story: null,
  };
  const node = (id) => {
    if (!m.nodes.has(id)) m.nodes.set(id, { id, role: null, state: "waiting", steps: [], stepCount: 0, stopReason: null, review: null, fixRounds: 0, context: null });
    return m.nodes.get(id);
  };
  for (const e of events) {
    if (m.started === null && typeof e.ts === "number") m.started = e.ts;
    switch (e.kind) {
      case "run_start":
        m.goal = e.goal ?? m.goal;
        m.repo = e.repo ?? m.repo;
        if (e.mode === "plan") m.mode = "plan";
        break;
      case "plan":
        m.planned = true;
        (e.subtasks || []).forEach((id, i) => {
          const n = node(id);
          n.role = n.role || (e.roles || [])[i] || null;
        });
        break;
      case "plan_error":
        m.planErrors.push(e.error || `attempt ${e.attempt}`);
        break;
      case "route":
        if (e.subtask) node(e.subtask).role = e.chosen || node(e.subtask).role;
        break;
      case "subtask_start": {
        const n = node(e.subtask);
        n.state = "active";
        n.role = e.role || n.role;
        break;
      }
      case "step": {
        if (!e.team_id) break; // a team run outside the studio's subtasks
        const n = node(e.team_id);
        n.stepCount = Math.max(n.stepCount, e.step || n.steps.length + 1);
        n.steps.push({ step: e.step, tool: e.tool, ok: e.ok !== false });
        if (n.steps.length > 40) n.steps.shift();
        break;
      }
      case "context": {
        if (e.team_id) {
          const n = node(e.team_id);
          n.context = { prompt_tokens: e.prompt_tokens, max_context: e.max_context };
        }
        break;
      }
      case "review": {
        m.reviewed = true;
        const n = node(e.subtask);
        n.review = { verdict: e.verdict, findings: e.findings ?? 0, blocking: e.blocking ?? 0, round: e.round ?? 1 };
        break;
      }
      case "fix_round": {
        const n = node(e.subtask);
        n.fixRounds = e.round ?? n.fixRounds + 1;
        n.state = "active"; // the role is sent back for the reviewer's blocking findings
        break;
      }
      case "follow_up":
        node(e.subtask).state = "active";
        break;
      case "subtask_done": {
        const n = node(e.subtask);
        n.state = e.ok ? "done" : "failed";
        n.stopReason = e.stop_reason || null;
        n.stepCount = e.steps ?? n.stepCount;
        break;
      }
      case "synthesis":
        m.synthesis = { chars: e.chars, open: e.open_questions ?? 0 };
        break;
      case "run_done":
        m.done = { ok: e.ok, tests: e.tests, wallMs: e.wall_ms };
        break;
      case "story_start":
      case "stage":
      case "layer_built":
      case "blocked":
      case "published":
      case "story_done":
        m.story = m.story || { stage: null, blocked: null, layers: [], prs: [] };
        if (e.stage) m.story.stage = e.stage;
        if (e.kind === "blocked") m.story.blocked = e.reason || e.blocked || "blocked";
        if (e.kind === "story_done" && e.blocked) m.story.blocked = e.blocked;
        if (e.kind === "layer_built") m.story.layers.push({ layer: e.layer, branch: e.branch });
        if (e.kind === "published") m.story.prs = e.prs || [];
        break;
      default:
        break;
    }
  }
  return m;
}

function shortRepo(path) {
  return path ? String(path).split("/").filter(Boolean).slice(-1)[0] : "";
}

function summarize(e) {
  const skip = new Set(["ts", "run_id", "kind", "probabilities", "text", "assumptions"]);
  return Object.entries(e)
    .filter(([k, v]) => !skip.has(k) && v !== null && v !== undefined && typeof v !== "object")
    .map(([k, v]) => `${k}=${String(v).slice(0, 60)}`)
    .join(" ");
}

export function render(root, [rawId], ctx) {
  const id = decodeURIComponent(rawId);
  let events = [];
  let since = 0;
  let state = "running";
  let result = null;
  let error = null;
  let selected = null;
  let gone = false;
  const abort = new AbortController();

  const heading = h("h1", { class: "h1 ellipsis-2" }, id);
  const meta = h("div", { class: "mono small muted" }, "waiting for the first event…");
  const status = h("div", { class: "row" });
  const stop = h("button", { type: "button", class: "btn danger small" }, "Stop job");
  const treeBox = h("div", { class: "tree" });
  const legend = h(
    "ul",
    { class: "legend", "aria-label": "Legend" },
    h("li", null, h("span", { class: "lg lg-active" }), "Working now"),
    h("li", null, h("span", { class: "lg lg-partial" }), "Stopped short"),
    h("li", null, h("span", { class: "lg lg-review" }), "Reviewer"),
    h("li", null, h("span", { class: "lg lg-wait" }), "Waiting"),
  );
  const detail = h("section", { class: "card pad stack", style: { gap: "10px" } });
  const resultBox = h("section", { class: "card pad stack", style: { gap: "10px" }, hidden: true });
  const timeline = h("ol", { class: "timeline mono small" });

  stop.addEventListener("click", async () => {
    stop.disabled = true;
    try {
      await request(`/v1/airlie/jobs/${encodeURIComponent(id)}`, { method: "DELETE" });
    } catch (e) {
      if (e?.name !== "AuthError") error = `Could not stop: ${e.message}`;
      stop.disabled = false;
      paint();
    }
  });

  root.append(
    h(
      "div",
      { class: "page" },
      h(
        "div",
        { class: "row spread", style: { alignItems: "flex-end" } },
        h("div", { class: "stack", style: { gap: "4px", minWidth: 0, flex: "1 1 320px" } }, h("a", { href: "#/workflows", class: "small" }, "← Workflows"), heading, meta),
        h("div", { class: "row" }, status, stop),
      ),
      h(
        "div",
        { class: "job-grid" },
        h("section", { class: "card pad stack" }, h("div", { class: "row spread" }, h("h2", { class: "h2" }, "Agents"), h("span", { class: "small muted" }, "tap a circle for its steps")), treeBox, legend),
        h("aside", { class: "stack", style: { gap: "20px" } }, detail, resultBox),
      ),
      h("section", { class: "card pad stack" }, h("h2", { class: "h2" }, "Events"), timeline),
    ),
  );

  // ---- drawing ----
  function nodeButton(key, label, sub, cls) {
    const b = h("button", { type: "button", class: `node ${cls}`, "aria-pressed": String(selected === key), onclick: () => select(key) }, h("strong", null, label), h("span", null, sub));
    return b;
  }

  function select(key) {
    selected = key;
    paint();
  }

  function glyph(n) {
    if (n.state === "active") return `● step ${n.stepCount || 1}`;
    if (n.state === "done") return `✓ ${n.stepCount} step${n.stepCount === 1 ? "" : "s"}`;
    if (n.state === "failed") return `! ${n.stopReason || "failed"}`;
    return "○ waiting";
  }

  function drawTree(m) {
    clear(treeBox);
    const chiefSub = m.planned ? "✓ plan" : m.planErrors.length ? `! retry ${m.planErrors.length}` : state === "running" ? "● planning" : "○";
    const chiefCls = m.planned ? "" : state === "running" ? "active" : m.planErrors.length ? "partial" : "";
    const rows = [h("div", { class: "tree-row" }, nodeButton("chief", "Chief", chiefSub, chiefCls))];
    const nodes = [...m.nodes.values()];
    if (nodes.length) {
      rows.push(h("div", { class: "seg" }));
      rows.push(
        h(
          "div",
          { class: "tree-row" },
          nodes.map((n) => nodeButton(n.id, n.id.length > 12 ? `${n.id.slice(0, 11)}…` : n.id, glyph(n), n.state === "active" ? "active" : n.state === "failed" ? "partial" : n.state === "waiting" ? "waiting" : "")),
        ),
      );
    }
    if (m.mode !== "plan" && (nodes.length || m.reviewed)) {
      const reviews = nodes.filter((n) => n.review);
      const findings = reviews.reduce((a, n) => a + (n.review.findings || 0), 0);
      const reviewSub = reviews.length ? `${findings} finding${findings === 1 ? "" : "s"}` : "○ waiting";
      const synthSub = m.synthesis ? "✓ done" : m.done ? "○ skipped" : "○ waiting";
      rows.push(h("div", { class: "seg" }));
      rows.push(h("div", { class: "tree-row" }, nodeButton("reviewer", "Reviewer", reviewSub, reviews.length ? "review" : "review waiting"), nodeButton("synthesis", "Synthesis", synthSub, m.synthesis ? "" : "waiting")));
    }
    if (m.story) {
      rows.push(h("div", { class: "seg" }));
      rows.push(h("div", { class: "tree-row" }, nodeButton("story", "Story", m.story.blocked ? "! blocked" : m.story.stage ? `✓ ${m.story.stage}` : "● starting", m.story.blocked ? "partial" : state === "running" ? "active" : "")));
    }
    append(treeBox, rows);
  }

  function drawDetail(m) {
    clear(detail);
    if (!selected || selected === "chief") {
      detail.append(h("h2", { class: "h2" }, "Chief"), h("div", { class: "muted" }, m.planned ? `Planned ${m.nodes.size} subtask${m.nodes.size === 1 ? "" : "s"}.` : "Writing the plan."));
      if (m.planErrors.length) detail.append(h("ul", { class: "small error" }, m.planErrors.map((e) => h("li", null, e))));
      return;
    }
    if (selected === "reviewer") {
      const reviews = [...m.nodes.values()].filter((n) => n.review);
      detail.append(h("h2", { class: "h2" }, "Reviewer"), reviews.length ? h("ul", { class: "small" }, reviews.map((n) => h("li", null, `${n.id}: ${n.review.verdict}, ${n.review.findings} finding${n.review.findings === 1 ? "" : "s"} (${n.review.blocking} blocking), round ${n.review.round}`))) : h("div", { class: "muted" }, "No review yet."));
      return;
    }
    if (selected === "synthesis") {
      detail.append(h("h2", { class: "h2" }, "Synthesis"), h("div", { class: "muted" }, m.synthesis ? `Written: ${m.synthesis.chars} characters, ${m.synthesis.open} open question${m.synthesis.open === 1 ? "" : "s"}.` : "Waits for every subtask."));
      return;
    }
    if (selected === "story") {
      detail.append(h("h2", { class: "h2" }, "Story"), h("div", null, `Stage: ${m.story?.stage || "starting"}`), m.story?.blocked ? h("div", { class: "error" }, m.story.blocked) : null, m.story?.layers.length ? h("ul", { class: "small mono" }, m.story.layers.map((l) => h("li", null, `layer ${l.layer}: ${l.branch}`))) : null);
      return;
    }
    const n = m.nodes.get(selected);
    if (!n) return;
    detail.append(
      h("h2", { class: "h2" }, n.id),
      h(
        "dl",
        { class: "facts" },
        h("div", null, h("dt", null, "Role"), h("dd", { class: "mono" }, n.role || "routing…")),
        h("div", null, h("dt", null, "Steps"), h("dd", { class: "mono" }, String(n.stepCount))),
        h("div", null, h("dt", null, "State"), h("dd", null, glyph(n))),
        n.context ? h("div", null, h("dt", null, "Context"), h("dd", null, `${n.context.prompt_tokens} / ${n.context.max_context} tokens`)) : null,
        h("div", null, h("dt", null, "Review"), h("dd", null, n.review ? `${n.review.verdict} · ${n.review.findings} findings` : n.fixRounds ? `fix round ${n.fixRounds}` : "—")),
      ),
      h("h3", { class: "h3" }, "Latest steps"),
      n.steps.length
        ? h("ol", { class: "steps mono small" }, n.steps.slice(-STEPS_SHOWN).map((s) => h("li", null, `${s.tool || "?"} · `, h("span", { class: s.ok ? "muted" : "error" }, s.ok ? "ok" : "error"))))
        : h("div", { class: "muted small" }, "No steps yet."),
    );
  }

  function drawResult(m) {
    clear(resultBox);
    resultBox.hidden = state === "running" && !error;
    if (error) resultBox.append(h("div", { class: "notice error" }, error));
    if (state === "running") return;
    if (state === "failed") {
      resultBox.append(h("h2", { class: "h2" }, "Failed"), h("div", { class: "notice error" }, result?.error || "The job failed without a message."));
      return;
    }
    if (state === "cancelled") {
      resultBox.append(h("h2", { class: "h2" }, "Stopped"), h("div", { class: "muted" }, "You stopped this job. Its run directory stays for inspection."));
      return;
    }
    const r = result || {};
    if (r.type === "plan" && r.plan) {
      resultBox.append(
        h("h2", { class: "h2" }, "Plan"),
        planCard(r.plan, {
          onApprove: async (plan) => {
            if (!m.repo) throw new Error("This plan's repository is unknown; start it again from Start new work.");
            const out = await startRun({ goal: m.goal || id, repo: m.repo, plan });
            noteJobInChat({ job_id: out.job_id, kind: out.kind, label: `${shortRepo(m.repo)} · studio run` });
            ctx.go(`#/jobs/${encodeURIComponent(out.job_id)}`);
          },
          onChange: () => {
            try {
              sessionStorage.setItem("airlie.chat.draft", `Change the plan for "${m.goal || id}": `);
            } catch {
              /* nothing kept */
            }
            ctx.go("#/");
          },
        }),
      );
      return;
    }
    if (r.type === "answer") {
      resultBox.append(h("h2", { class: "h2" }, r.failed ? "No plan" : "Answer"), h("div", { class: r.failed ? "notice" : "" }, markdown(r.answer || "")));
      return;
    }
    const okChip = r.ok === true ? chip("ok", "Passed") : r.ok === false ? chip("err", "Did not pass") : null;
    resultBox.append(h("div", { class: "row spread" }, h("h2", { class: "h2" }, "Result"), okChip));
    if (r.branch) resultBox.append(h("div", { class: "small muted" }, `The branch is in the Studio's checkout${m.repo ? ` at ${m.repo}` : ""}. Fetch it to try it:`), h("code", { class: "mono codebox" }, `git fetch <studio> ${r.branch}`));
    if (r.tests && typeof r.tests === "object") resultBox.append(h("div", { class: "small muted" }, `Tests: ${Object.entries(r.tests).map(([k, v]) => `${k} ${v ? "✓" : "✗"}`).join(", ")}`));
    if (r.final_answer) resultBox.append(h("div", { class: "answer" }, markdown(r.final_answer)));
    const rest = Object.entries(r).filter(([k, v]) => !["ok", "branch", "tests", "final_answer", "run_id", "type"].includes(k) && (typeof v !== "object" || v === null));
    if (rest.length) resultBox.append(h("dl", { class: "facts" }, rest.map(([k, v]) => h("div", null, h("dt", null, k), h("dd", { class: "mono" }, String(v))))));
  }

  function drawTimeline() {
    clear(timeline);
    const recent = events.slice(-30).reverse();
    append(timeline, [
      recent.length ? null : h("li", { class: "muted" }, "No events yet."),
      recent.map((e) => h("li", null, h("span", { class: "muted" }, typeof e.ts === "number" ? clock(e.ts) : "—"), " ", h("strong", null, e.kind), " ", summarize(e))),
    ]);
  }

  function paint() {
    const m = digest(events);
    heading.textContent = m.goal || id;
    const bits = [m.mode === "plan" ? "studio plan" : m.story ? "story" : "studio run", id, shortRepo(m.repo), m.started ? `started ${clock(m.started)}` : null].filter(Boolean);
    meta.textContent = bits.join(" · ");
    const elapsed = m.started ? ago((m.done?.wallMs ?? Date.now() - m.started * 1000)) : null;
    append(clear(status), [stateChip(state), elapsed ? h("span", { class: "mono small muted" }, elapsed) : null]);
    stop.hidden = state !== "running";
    drawTree(m);
    drawDetail(m);
    drawResult(m);
    drawTimeline();
  }

  // ---- following ----
  async function follow() {
    while (!gone) {
      let body;
      try {
        body = await request(`/v1/airlie/jobs/${encodeURIComponent(id)}?since=${since}&wait=${WAIT}`, { signal: abort.signal });
      } catch (e) {
        if (gone || e?.name === "AbortError" || e?.name === "AuthError") return;
        if (e?.status === 404) {
          state = "failed";
          result = { error: "This job isn't in this airlie: jobs do not survive a restart of serve. Its run directory, if it got that far, is listed under Recent results." };
          paint();
          return;
        }
        error = `Lost airlie: ${e.message} (retrying)`;
        paint();
        await new Promise((r) => setTimeout(r, 3000));
        continue;
      }
      error = null;
      events.push(...(body.events || []));
      since = body.next ?? since;
      state = body.state;
      result = body.state === "failed" ? { error: body.error } : (body.result ?? result);
      paint();
      if (state !== "running") return;
    }
  }

  paint();
  follow();
  const tick = setInterval(() => {
    if (state === "running") paint();
  }, 1000);
  return () => {
    gone = true;
    clearInterval(tick);
    abort.abort();
  };
}
