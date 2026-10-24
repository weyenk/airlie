// One story's workflow: its stages left to right, a lane of jobs per layer, and the selected job's inputs,
// outputs, retries and numbers (GET /v1/airlie/stories/{key}/flow, then .../flow/{node}/{job}).
import { request } from "../api.js";
import { chip, clear, h } from "../dom.js";

export const title = "Workflow";

const REFRESH_MS = 10000;
const STATUS_CHIP = { passed: "ok", failed: "err", running: "run", pending: "mute", skipped: "mute" };

/** 3, 217 and 3700 seconds as "3 s", "3 min 37 s" and "1 h 1 min". */
export function span(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}

function count(n) {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function rate(v) {
  return v === null || v === undefined ? null : `${v} tok/s`;
}

/** The numbers of a job, as [label, value] pairs: only those it has. */
export function stats(job) {
  const t = job.tokens;
  const rows = [["Attempts", String(job.attempts)]];
  if (job.fix_rounds) rows.push(["Fix rounds", String(job.fix_rounds)]);
  if (job.duration_s !== null && job.duration_s !== undefined) rows.push(["Duration", span(job.duration_s)]);
  if (t) {
    rows.push(["Steps", String(t.steps)], ["Prompt tokens", count(t.prompt)], ["Completion tokens", count(t.completion)]);
    rows.push(["Generation", rate(t.tps)], ["End to end", rate(t.effective_tps)], ["Prefill", rate(t.prefill_tps)]);
    if (t.model) rows.push(["Model", t.model]);
  }
  return rows.filter(([, v]) => v);
}

function jobButton(node, job, selected, pick) {
  const sub = [job.role, job.duration_s ? span(job.duration_s) : null].filter(Boolean).join(" · ");
  return h(
    "button",
    {
      type: "button",
      class: `wf-job ${job.status}`,
      "aria-pressed": selected ? "true" : "false",
      dataset: { node: node.id, job: job.id },
      onclick: () => pick(node.id, job.id),
    },
    h("span", { class: "wf-top" }, h("span", { class: "wf-name" }, job.label), job.attempts > 1 ? chip("warn", `${job.attempts}x`) : null),
    h("span", { class: "wf-sub mono" }, sub || job.status),
  );
}

function connector() {
  return h("span", { class: "wf-conn", "aria-hidden": "true" }, "→");
}

function laneHead(node) {
  return h(
    "div",
    { class: "wf-lane-head" },
    h("span", { class: `wf-dot ${node.status}` }),
    h("strong", null, `${node.label}${node.title ? ` · ${node.title}` : ""}`),
    node.attempts > 1 ? chip("warn", `${node.attempts} attempts`) : null,
    node.acceptance?.length ? h("span", { class: "mono small muted" }, node.acceptance.join(", ")) : null,
  );
}

export function render(root, [key, nodeParam, jobParam], ctx) {
  const heading = h("div", { class: "stack", style: { gap: "4px", minWidth: 0 } });
  const stagesEl = h("div", { class: "wf-stages" });
  const detail = h("section", { class: "card pad stack", "aria-live": "polite" });
  root.append(
    h(
      "div",
      { class: "page" },
      h("section", { class: "card pad stack" }, heading),
      h("section", { class: "card pad stack" }, h("h2", { class: "h2" }, "Pipeline"), h("div", { class: "wf-flow" }, stagesEl)),
      detail,
    ),
  );

  let flow = null;
  let sel = nodeParam && jobParam ? [nodeParam, jobParam] : null;
  let gone = false;
  let loaded = false;
  let timer = null;

  function jobOf(nodeId, jobId) {
    const node = flow?.nodes.find((n) => n.id === nodeId);
    return node ? [node, node.jobs.find((j) => j.id === jobId)] : [null, null];
  }

  function firstInterest() {
    // the failed job if the story stopped, else the running one, else the last that ran
    const all = flow.nodes.flatMap((n) => n.jobs.map((j) => [n.id, j.id, j.status]));
    const pick = all.find((x) => x[2] === "failed") || all.find((x) => x[2] === "running") || [...all].reverse().find((x) => x[2] === "passed");
    return pick ? [pick[0], pick[1]] : null;
  }

  function pick(nodeId, jobId) {
    sel = [nodeId, jobId];
    drawFlow();
    loadDetail();
  }

  function drawHeading() {
    clear(heading).append(
      h("a", { href: "#/workflows", class: "small" }, "← Workflows"),
      h(
        "div",
        { class: "row" },
        h("span", { class: "mono muted" }, flow.key),
        flow.blocked ? chip("warn", "Blocked") : chip(flow.stage === "review" ? "ok" : "run", flow.stage === "review" ? "Published" : `After ${flow.stage}`),
        h("a", { href: `#/board/${encodeURIComponent(flow.key)}`, class: "small" }, "Board card"),
      ),
      h("h1", { class: "h1" }, flow.title || flow.key),
      flow.blocked ? h("div", { class: "error small" }, flow.blocked) : null,
    );
  }

  function drawFlow() {
    clear(stagesEl);
    flow.nodes.forEach((node, i) => {
      const prev = flow.nodes[i - 1];
      const isLayer = node.kind === "layer";
      const stack = h("div", { class: "wf-col" });
      if (isLayer) {
        if (!prev || prev.kind !== "layer") {
          stagesEl.append(connector());
          const lanes = h("div", { class: "wf-lanes" });
          stagesEl.append(lanes);
        }
        const lanes = stagesEl.lastElementChild;
        const row = h("div", { class: "wf-lane-jobs" });
        node.jobs.forEach((job, k) => {
          if (k) row.append(connector());
          row.append(jobButton(node, job, sel && sel[0] === node.id && sel[1] === job.id, pick));
        });
        lanes.append(h("div", { class: `wf-lane ${node.status}`, dataset: { node: node.id } }, laneHead(node), row));
        return;
      }
      if (node.jobs.length === 0) return; // a stage that never ran (no survey)
      const same = prev && prev.kind === "stage" && node.id === "survey";
      if (same) {
        stagesEl.lastElementChild.append(...node.jobs.map((j) => jobButton(node, j, sel && sel[0] === node.id && sel[1] === j.id, pick)));
        return;
      }
      if (stagesEl.childElementCount) stagesEl.append(connector());
      stack.append(...node.jobs.map((j) => jobButton(node, j, sel && sel[0] === node.id && sel[1] === j.id, pick)));
      stagesEl.append(stack);
    });
  }

  function steps(list) {
    if (!list.length) return null;
    return h(
      "div",
      { class: "stack", style: { gap: "0" } },
      list.map((s) =>
        h(
          "details",
          { class: "wf-step" },
          h(
            "summary",
            null,
            h("span", { class: "mono muted" }, s.attempt ? `r${s.attempt}·${s.n}` : String(s.n)),
            h("span", { class: "mono" }, s.tool || "(no tool)"),
            h("span", { class: "mono small muted ellipsis" }, s.arguments || ""),
            s.ok === false ? chip("err", "failed") : null,
          ),
          s.reasoning ? h("pre", { class: "wf-pre" }, `thinking\n${s.reasoning}`) : null,
          s.assistant_text ? h("pre", { class: "wf-pre" }, s.assistant_text) : null,
          h("pre", { class: "wf-pre" }, s.observation || ""),
        ),
      ),
    );
  }

  function inputOf(d) {
    if (!d.input) return h("p", { class: "small muted", style: { margin: 0 } }, d.input_note || "");
    return h(
      "div",
      { class: "stack", style: { gap: "8px" } },
      d.input.messages.map((m) => h("div", null, h("div", { class: "small muted mono" }, m.role), h("pre", { class: "wf-pre" }, m.content || ""))),
    );
  }

  function outputOf(d) {
    const o = d.output || {};
    const parts = [];
    if (o.story) parts.push(["story", o.story]);
    if (o.layers) parts.push(["layers", o.layers]);
    if (o.layer) parts.push(["layer brief", o.layer]);
    if (o.built) parts.push(["built", o.built]);
    if (o.survey) parts.push(["survey notes", o.survey]);
    if (o.final_text) parts.push(["final reply", o.final_text]);
    if (!parts.length) return h("p", { class: "small muted", style: { margin: 0 } }, "Nothing recorded.");
    return h(
      "div",
      { class: "stack", style: { gap: "8px" } },
      parts.map(([label, value]) => h("div", null, h("div", { class: "small muted mono" }, label), h("pre", { class: "wf-pre" }, typeof value === "string" ? value : JSON.stringify(value, null, 2)))),
    );
  }

  async function loadDetail() {
    if (!sel) {
      clear(detail).append(h("p", { class: "muted", style: { margin: 0 } }, "Nothing has run yet."));
      return;
    }
    const [nodeId, jobId] = sel;
    let d;
    try {
      d = await request(`/v1/airlie/stories/${encodeURIComponent(key)}/flow/${encodeURIComponent(nodeId)}/${encodeURIComponent(jobId)}`);
    } catch (e) {
      if (e?.name === "AuthError") return;
      clear(detail).append(h("div", { class: "error" }, e.message));
      return;
    }
    if (gone || !sel || sel[0] !== nodeId || sel[1] !== jobId) return;
    const [node] = jobOf(nodeId, jobId);
    const job = d.job;
    clear(detail).append(
      h(
        "div",
        { class: "row spread" },
        h("div", { class: "row" }, h("h2", { class: "h2", style: { margin: 0 } }, `${node?.label || nodeId} / ${job.label}`), chip(STATUS_CHIP[job.status] || "mute", job.status), job.role ? chip("mute", job.role) : null),
        job.studio_run ? h("span", { class: "small muted mono" }, `run ${job.studio_run}`) : null,
      ),
      h("div", { class: "wf-stats" }, stats(job).map(([k, v]) => h("div", { class: "wf-stat" }, h("div", { class: "mono" }, v), h("div", { class: "small muted" }, k)))),
      job.retries?.length
        ? h("div", null, h("h3", { class: "h3" }, "Why it retried"), job.retries.map((r) => h("div", { class: "wf-retry" }, h("code", null, r.kind), h("span", null, r.detail))))
        : null,
      h("div", { class: "wf-io" }, h("div", null, h("h3", { class: "h3" }, "Input: what it was given"), inputOf(d)), h("div", null, h("h3", { class: "h3" }, "Output: what it produced"), outputOf(d))),
      d.steps.length ? h("div", null, h("h3", { class: "h3" }, `Steps (${d.steps.length})`), steps(d.steps)) : null,
    );
  }

  async function refresh() {
    try {
      flow = await request(`/v1/airlie/stories/${encodeURIComponent(key)}/flow`);
    } catch (e) {
      if (e?.name === "AuthError") return;
      clear(heading).append(h("a", { href: "#/workflows", class: "small" }, "← Workflows"), h("div", { class: "error" }, e?.status === 404 ? `No story ${key}.` : e.message));
      return;
    }
    if (gone) return;
    const first = !loaded;
    loaded = true;
    if (sel === null || !jobOf(...sel)[1]) sel = firstInterest();
    drawHeading();
    drawFlow();
    if (first) loadDetail();
    else if (flow.nodes.some((n) => n.jobs.some((j) => j.status === "running"))) loadDetail();
  }

  refresh();
  timer = setInterval(refresh, REFRESH_MS);
  return () => {
    gone = true;
    clearInterval(timer);
  };
}
