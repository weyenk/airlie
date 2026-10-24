// Topology: one run's agents as a live node tree, shaped by the topology that ran it (studio: chief →
// specialists → reviewer → synthesis; fork: the stuck agent and its forks; teams: the teams and the judge;
// single: one agent). The server builds the tree from the run's events (GET /v1/airlie/runs/{id}/tree) and
// holds each poll until the log grows; this screen lays it out, marks who is working and the path of the
// work right now, and lists the hand-offs with what went across. Tap a circle for its role, model, skill,
// steps, tokens and last events.
import { request } from "../api.js";
import { ago, append, chip, clear, clock, h } from "../dom.js";

export const title = "Topology";

const WAIT = 20; // seconds a long poll is held
const SVG = "http://www.w3.org/2000/svg";

const ABOUT = {
  studio: "The chief plans and delegates each subtask to a specialist. A reviewer from another model family checks the writers' work, and the chief writes the answer.",
  fork: "One agent works alone. When it gets stuck, airlie forks it with different hypotheses and carries on with the branch that gets unstuck.",
  teams: "Teams work the same task in parallel; the judge picks the best result.",
  single: "One agent does the whole job.",
};

const STATE_WORDS = { active: "working", idle: "waiting on others", waiting: "not started", done: "finished", failed: "stopped short", stuck: "stuck: forked" };

/** Rows of node ids, top to bottom, as the topology arranges them. */
function rows(tree) {
  const ids = tree.nodes.map((n) => n.id);
  const kind = (k) => tree.nodes.filter((n) => n.kind === k).map((n) => n.id);
  if (tree.topology === "studio") {
    return [[...kind("chief"), ...kind("synthesis")], kind("role"), kind("reviewer")].filter((r) => r.length);
  }
  if (tree.topology === "fork") {
    const parent = new Map(tree.edges.filter((e) => e.what === "hypothesis").map((e) => [e.dst, e.src]));
    const depth = (id, seen = 0) => (parent.has(id) && seen < 50 ? 1 + depth(parent.get(id), seen + 1) : 0);
    const out = [];
    for (const id of ids) (out[depth(id)] ||= []).push(id);
    return out.filter(Boolean);
  }
  if (tree.topology === "teams") return [kind("team"), kind("aggregate")].filter((r) => r.length);
  return ids.length ? [ids] : [];
}

/** Where each circle goes and the elbow paths between them, in a box `width` wide. */
export function layout(tree, width) {
  const narrow = width < 600;
  const cols = narrow ? 4 : 7;
  const single = tree.nodes.length === 1;
  const r = single ? (narrow ? 70 : 110) : narrow ? 32 : 50;
  const gap = narrow ? 34 : 56;
  const wrapped = [];
  for (const row of rows(tree)) for (let i = 0; i < row.length; i += cols) wrapped.push(row.slice(i, i + cols));
  const pos = new Map();
  let y = r + 12;
  for (const row of wrapped) {
    const step = width / row.length;
    row.forEach((id, i) => pos.set(id, { x: step * (i + 0.5), y, r }));
    y += 2 * r + gap;
  }
  const height = Math.max(y - gap + 12, 2 * r + 24);
  // one path per pair of nodes that handed something across (and, in a studio, the chief to every subtask)
  const pairs = new Map();
  const pair = (a, b, on) => {
    if (!pos.has(a) || !pos.has(b) || a === b) return;
    const key = [a, b].sort().join("\u0000");
    const p = pairs.get(key) || { a, b, on: false, edges: [] };
    p.on = p.on || on;
    pairs.set(key, p);
    return p;
  };
  if (tree.topology === "studio") tree.nodes.filter((n) => n.kind === "role").forEach((n) => pair("chief", n.id, false));
  for (const e of tree.edges) pair(e.src, e.dst, e.active)?.edges.push(e);
  const lay0 = r + 12;
  const paths = [...pairs.values()].map((p) => {
    const [top, low] = pos.get(p.a).y <= pos.get(p.b).y ? [pos.get(p.a), pos.get(p.b)] : [pos.get(p.b), pos.get(p.a)];
    let d;
    if (Math.abs(top.y - low.y) < 1 && top.y === lay0) {
      d = `M${top.x} ${top.y} H${low.x}`; // the top row: the chief and its own synthesis, side by side
    } else if (Math.abs(top.y - low.y) < 1) {
      const under = top.y + top.r + gap / 2.5; // same row: under both
      d = `M${top.x} ${top.y} V${under} H${low.x} V${low.y}`;
    } else {
      const mid = top.y + top.r + (low.y - low.r - top.y - top.r) / 2;
      d = `M${top.x} ${top.y} V${mid} H${low.x} V${low.y}`;
    }
    return { d, on: p.on, edges: p.edges };
  });
  return { width, height, pos, paths, narrow };
}

function svg(tag, attrs, ...children) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined && v !== false) el.setAttribute(k, String(v));
  for (const c of children.flat()) if (c) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

function glyph(n) {
  if (n.state === "active") return n.current ? `● ${n.current.tool || "step"}` : "● working";
  if (n.state === "idle") return "◐ waiting";
  if (n.state === "done") return n.won ? "✓ won" : n.steps ? `✓ ${n.steps} step${n.steps === 1 ? "" : "s"}` : "✓ done";
  if (n.state === "failed") return `! ${n.stop_reason || "stopped"}`;
  if (n.state === "stuck") return "! stuck";
  return "○ waiting";
}

function short(text, max) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function label(tree, id) {
  return tree.nodes.find((n) => n.id === id)?.label || id;
}

export function render(root, [rawId], ctx) {
  const id = rawId ? decodeURIComponent(rawId) : null;
  let tree = null;
  let since = 0;
  let selected = null;
  let error = null;
  let gone = false;
  const abort = new AbortController();

  const picker = h("select", { class: "field", "aria-label": "Run" }, h("option", { value: "" }, "Loading runs…"));
  picker.addEventListener("change", () => picker.value && ctx.go(`#/topology/${encodeURIComponent(picker.value)}`));
  const about = h("p", { class: "muted", style: { margin: 0 } });
  const status = h("div", { class: "row" });
  const heading = h("div", { class: "small muted ellipsis-2" });
  const diagram = h("div", { class: "topo-diagram", role: "group", "aria-label": "Agents" });
  const detail = h("section", { class: "card pad stack", style: { gap: "10px" }, "aria-live": "polite" });
  const handoffs = h("ol", { class: "topo-handoffs" });
  const handoffTitle = h("h2", { class: "h2" }, "Hand-offs");
  const legend = h(
    "ul",
    { class: "legend", "aria-label": "Legend" },
    h("li", null, h("span", { class: "lg-line" }), "Path of the work right now"),
    h("li", null, h("span", { class: "lg lg-active" }), "Working now"),
    h("li", null, h("span", { class: "lg lg-review" }), "Reviewer (other model family)"),
    h("li", null, h("span", { class: "lg lg-stuck" }), "Stuck: handed to a fork"),
    h("li", null, h("span", { class: "lg lg-wait" }), "Not started"),
  );

  root.append(
    h(
      "div",
      { class: "page" },
      h(
        "div",
        { class: "topo-grid" },
        h(
          "div",
          { class: "stack topo-side", style: { gap: "14px" } },
          h("h1", { class: "h1" }, "Topology"),
          h("label", { class: "stack", style: { gap: "6px" } }, h("span", { class: "small muted" }, "Run"), picker),
          heading,
          status,
          about,
          legend,
        ),
        h("section", { class: "card pad stack topo-main" }, diagram, h("div", { class: "small muted" }, "Tap a circle for its role, model, steps and tokens.")),
      ),
      h("div", { class: "job-grid" }, h("section", { class: "card pad stack" }, handoffTitle, handoffs), h("aside", { class: "stack" }, detail)),
    ),
  );

  // ---- the run picker ----
  async function loadPicker() {
    let jobs = [];
    let runs = [];
    try {
      [{ jobs }, { runs }] = await Promise.all([request("/v1/airlie/jobs"), request("/v1/airlie/runs?limit=30")]);
    } catch (e) {
      if (e?.name === "AuthError") return;
      error = `Could not list runs: ${e.message}`;
      paint();
      return;
    }
    if (gone) return;
    const running = new Set(jobs.filter((j) => j.state === "running").map((j) => j.id));
    const opts = runs.map((r) => {
      const when = r.started ? clock(r.started) : "";
      const mark = running.has(r.id) ? "● " : r.ok === null ? "… " : r.ok ? "✓ " : "✗ ";
      return h("option", { value: r.id }, `${mark}${short(r.goal || r.id, 60)}${when ? ` · ${when}` : ""}`);
    });
    if (id && !runs.some((r) => r.id === id)) opts.unshift(h("option", { value: id }, id));
    append(clear(picker), [opts.length ? null : h("option", { value: "" }, "No runs yet"), opts]);
    if (id) picker.value = id;
    else if (runs.length) {
      const first = runs.find((r) => running.has(r.id)) || runs[0];
      ctx.go(`#/topology/${encodeURIComponent(first.id)}`);
    }
  }

  // ---- drawing ----
  function select(key) {
    selected = selected === key ? null : key;
    paint();
  }

  function drawDiagram() {
    clear(diagram);
    if (!tree || !tree.nodes.length) {
      diagram.append(h("div", { class: "empty" }, id ? (tree ? "No agents have started yet." : "Loading…") : "Pick a run to see its agents."));
      return;
    }
    const width = Math.max(300, Math.min(diagram.clientWidth || 900, 1100));
    const lay = layout(tree, width);
    const art = svg("svg", { viewBox: `0 0 ${lay.width} ${lay.height}`, width: "100%", class: "topo-svg", role: "presentation" });
    const defs = svg(
      "defs",
      null,
      ...[
        ["tg-node", "#e6f0fb", "#9dbfe5"],
        ["tg-review", "#eaf5e3", "#9ccb84"],
        ["tg-stuck", "#fbe1e1", "#e28f8f"],
      ].map(([gid, a, b]) => svg("radialGradient", { id: gid }, svg("stop", { offset: "0%", "stop-color": a }), svg("stop", { offset: "100%", "stop-color": b }))),
    );
    art.append(defs);
    for (const p of lay.paths) {
      const tip = p.edges.length ? p.edges.map((e) => `${label(tree, e.src)} → ${label(tree, e.dst)} (${e.what})${e.text ? `: ${e.text}` : ""}`).join("\n") : null;
      art.append(svg("path", { d: p.d, class: p.on ? "topo-seg on" : "topo-seg" }, tip ? svg("title", null, tip) : null));
    }
    for (const n of tree.nodes) {
      const at = lay.pos.get(n.id);
      if (!at) continue;
      const fill = n.kind === "reviewer" ? "tg-review" : n.state === "stuck" ? "tg-stuck" : "tg-node";
      const words = `${n.label}: ${STATE_WORDS[n.state] || n.state}${n.current ? `, step ${n.current.step} ${n.current.tool}` : ""}`;
      const g = svg(
        "g",
        { class: `topo-node ${n.state}${n.won ? " won" : ""}`, role: "button", tabindex: "0", "aria-label": words, "aria-pressed": String(selected === n.id), "data-node": n.id },
        svg("circle", { cx: at.x, cy: at.y, r: at.r, fill: `url(#${fill})` }),
        svg("text", { x: at.x, y: at.y - (lay.narrow ? 3 : 5), class: "topo-label", "font-size": lay.narrow ? 11 : 15 }, short(n.label, lay.narrow ? 9 : 12)),
        svg("text", { x: at.x, y: at.y + (lay.narrow ? 11 : 15), class: "topo-sub", "font-size": lay.narrow ? 9 : 12 }, short(glyph(n), lay.narrow ? 11 : 15)),
      );
      g.addEventListener("click", () => select(n.id));
      g.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          select(n.id);
        }
      });
      art.append(g);
    }
    diagram.append(art);
  }

  function drawDetail() {
    clear(detail);
    const n = tree?.nodes.find((x) => x.id === selected) || tree?.nodes.find((x) => x.state === "active") || tree?.nodes[0];
    if (!n) {
      detail.append(h("h2", { class: "h2" }, "Agent"), h("div", { class: "muted" }, "Nothing to show yet."));
      return;
    }
    const fact = (k, v) => (v === null || v === undefined || v === "" ? null : h("div", null, h("dt", null, k), h("dd", { class: "mono" }, String(v))));
    const tokens = n.tokens.prompt || n.tokens.completion ? `${n.tokens.prompt.toLocaleString()} in · ${n.tokens.completion.toLocaleString()} out` : null;
    append(detail, [
      h("div", { class: "row spread" }, h("h2", { class: "h2" }, n.label), chip(n.state === "active" ? "run" : n.state === "failed" || n.state === "stuck" ? "warn" : "mute", STATE_WORDS[n.state] || n.state)),
      n.id !== n.label.toLowerCase() ? h("div", { class: "small muted mono" }, n.id) : null,
      h(
        "dl",
        { class: "facts" },
        fact("Role", n.role),
        fact("Model", n.model),
        fact("Skill", n.skill),
        fact("Steps", n.steps),
        fact("Tokens", tokens),
        fact("Now", n.current && n.state === "active" ? `step ${n.current.step} · ${n.current.tool}` : null),
        fact("Fix rounds", n.fix_rounds || null),
        fact("Stopped", n.state === "failed" ? n.stop_reason : null),
      ),
      h("h3", { class: "h3" }, "Last events"),
      n.recent.length ? h("ol", { class: "steps mono small" }, n.recent.map((r) => h("li", null, r))) : h("div", { class: "muted small" }, "None yet."),
    ]);
  }

  function drawHandoffs() {
    clear(handoffs);
    const shown = (tree?.edges || []).filter((e) => !selected || e.src === selected || e.dst === selected);
    handoffTitle.textContent = selected ? `Hand-offs of ${label(tree, selected)}` : "Hand-offs";
    if (!shown.length) {
      handoffs.append(h("li", { class: "muted" }, tree?.edges.length ? "None for this agent." : "No hand-offs yet."));
      return;
    }
    append(
      handoffs,
      [...shown].reverse().map((e) =>
        h(
          "li",
          { class: e.active ? "on" : null },
          h(
            "div",
            { class: "row spread" },
            h("strong", null, `${label(tree, e.src)} → ${label(tree, e.dst)}`),
            h("span", { class: "small muted mono" }, [e.what, typeof e.ts === "number" ? clock(e.ts) : null].filter(Boolean).join(" · ")),
          ),
          e.text ? h("div", { class: "small topo-text" }, e.text) : null,
          e.active ? chip("run", "now") : null,
        ),
      ),
    );
  }

  function paint() {
    if (!tree) {
      about.textContent = "";
      append(clear(status), [error ? h("div", { class: "notice error" }, error) : null]);
      drawDiagram();
      drawDetail();
      drawHandoffs();
      return;
    }
    heading.textContent = [tree.goal, tree.repo ? String(tree.repo).split("/").filter(Boolean).pop() : null].filter(Boolean).join(" · ");
    about.textContent = ABOUT[tree.topology] || "";
    const stateChip =
      tree.state === "running" ? chip("run", tree.job === "running" ? "Running" : "Running (no end logged yet)") : tree.state === "done" ? (tree.ok === false ? chip("warn", "Did not pass") : chip("mute", "Finished")) : chip("warn", tree.state === "cancelled" ? "Stopped" : "Failed");
    const elapsed = tree.wall_ms ? ago(tree.wall_ms) : tree.started ? ago(Date.now() - tree.started * 1000) : null;
    append(clear(status), [stateChip, elapsed ? h("span", { class: "mono small muted" }, elapsed) : null, h("a", { class: "small", href: `#/jobs/${encodeURIComponent(id)}` }, "Events"), error ? h("div", { class: "notice error" }, error) : null]);
    drawDiagram();
    drawDetail();
    drawHandoffs();
  }

  // ---- following ----
  async function follow() {
    while (!gone && id) {
      let body;
      try {
        const wait = tree ? WAIT : 0;
        body = await request(`/v1/airlie/runs/${encodeURIComponent(id)}/tree?since=${since}&wait=${wait}`, { signal: abort.signal });
      } catch (e) {
        if (gone || e?.name === "AbortError" || e?.name === "AuthError") return;
        if (e?.status === 404) {
          error = "No run of that id in this airlie.";
          paint();
          return;
        }
        error = `Lost airlie: ${e.message} (retrying)`;
        paint();
        await new Promise((r) => setTimeout(r, 3000));
        continue;
      }
      error = null;
      tree = body;
      since = body.next ?? since;
      paint();
      if (tree.state !== "running") return;
    }
  }

  let resizeTimer = null;
  const onResize = () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(drawDiagram, 150);
  };
  window.addEventListener("resize", onResize);
  paint();
  loadPicker();
  follow();
  return () => {
    gone = true;
    abort.abort();
    clearTimeout(resizeTimer);
    window.removeEventListener("resize", onResize);
  };
}
