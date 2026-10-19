// The Metrics screen: a range picker, a slice-by select, four stat tiles and a hand-drawn SVG
// series chart of generation tok/s. Data comes from /v1/metrics/summary and /v1/metrics/series
// (the airmetrics module) behind the existing API key. Missing slice values and missing prefill
// render as "unknown"; empty series arrays render an empty chart, never a thrown error.
import { metricsSeries, metricsSummary } from "../api.js";
import { clear, h } from "../dom.js";

export const title = "Metrics";

const RANGES = [
  ["4 hours", 4 * 3600],
  ["24 hours", 24 * 3600],
  ["7 days", 7 * 86400],
  ["30 days", 30 * 86400],
  ["All time", null], // no `from`: the server takes everything it has
];

const SLICES = ["role", "model", "topology", "task", "agent"];

const TILES = [
  ["Generation tok/s", (s) => s.generation_tps, "tok/s"],
  ["Prefill tok/s", (s) => s.prefill_tps, "tok/s"],
  ["Stories", (s) => s.stories, ""],
  ["Fix rounds per layer", (s) => s.fix_rounds_per_layer, ""],
];

const COLORS = ["var(--accent)", "var(--warn)", "var(--ok)", "var(--err)", "var(--run)"];

function fmt(v, unit) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const text = Number.isInteger(n) ? String(n) : n.toFixed(1);
  return unit ? `${text} ${unit}`.trim() : text;
}

/** A slice value as the server returns it, or "unknown" when absent (AC10). */
function label(value) {
  return value ?? "unknown";
}

/** The range picker: a segmented button per option; the active one wins. */
function rangePicker(active, onPick) {
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Time range" });
  for (const [i, [text]] of RANGES.entries()) {
    const b = h("button", { type: "button", class: i === active ? "on" : "" }, text);
    b.addEventListener("click", () => onPick(i));
    seg.append(b);
  }
  return seg;
}

function sliceSelect(active, onPick) {
  const sel = h("select", { class: "field" }, SLICES.map((s) => h("option", { value: s, selected: s === active }, s)));
  sel.addEventListener("change", () => onPick(sel.value));
  return sel;
}

function tile(labelText, value) {
  return h("div", { class: "card kpi" }, h("span", { class: "sub" }, labelText), h("div", { class: "v mono" }, value));
}

/** One line per slice value from the series rows; the first slice also gets its p10-p90 band. */
function chart(svg, rows, sliceBy) {
  clear(svg);
  const W = 640;
  const H = 220;
  const L = 36;
  const B = 22;
  if (!Array.isArray(rows) || rows.length === 0) {
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.append(h("text", { x: L, y: H / 2 }, "No data in this range"));
    return;
  }
  const values = rows.map((r) => r.p50).filter((v) => typeof v === "number" && Number.isFinite(v));
  const lo = Math.min(0, ...values);
  const hi = Math.max(...values, 1);
  const t0 = rows[0].t;
  const t1 = rows[rows.length - 1].t;
  const span = t1 > t0 ? t1 - t0 : 1;
  const x = (t) => L + ((t - t0) / span) * (W - L - 8);
  const y = (v) => 10 + (H - B - 10) * (1 - (v - lo) / (hi - lo || 1));
  const NS = "http://www.w3.org/2000/svg";
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  for (let g = 0; g <= 4; g++) {
    const gy = 10 + (H - B - 10) * (g / 4);
    const line = document.createElementNS(NS, "line");
    line.setAttribute("x1", L);
    line.setAttribute("x2", W);
    line.setAttribute("y1", gy);
    line.setAttribute("y2", gy);
    line.setAttribute("stroke", "var(--line2)");
    svg.append(line);
  }
  const seen = new Set();
  const groups = [];
  for (const r of rows) {
    const v = label(r[sliceBy]);
    if (!seen.has(v)) {
      seen.add(v);
      groups.push({ v, pts: [] });
    }
  }
  for (const r of rows) {
    const g = groups.find((x2) => x2.v === label(r[sliceBy]));
    if (g && typeof r.p50 === "number" && Number.isFinite(r.p50)) g.pts.push([r.t, r.p50, r.p10, r.p90]);
  }
  groups.forEach((g, i) => {
    const color = COLORS[i % COLORS.length];
    if (i === 0 && g.pts.length > 1) {
      // the p10-p90 band on the first slice: a closed polygon between its p10 and p90
      const loPts = g.pts.map(([t, , p10]) => `${x(t).toFixed(0)},${y(p10 ?? 0).toFixed(0)}`);
      const hiPts = g.pts.map(([t, , , p90]) => `${x(t).toFixed(0)},${y(p90 ?? 0).toFixed(0)}`).reverse();
      const poly = document.createElementNS(NS, "polygon");
      poly.setAttribute("points", loPts.concat(hiPts).join(" "));
      poly.setAttribute("fill", color);
      poly.setAttribute("opacity", "0.12");
      svg.append(poly);
    }
    const pl = document.createElementNS(NS, "polyline");
    pl.setAttribute("points", g.pts.map(([t, v]) => `${x(t).toFixed(0)},${y(v).toFixed(0)}`).join(" "));
    pl.setAttribute("fill", "none");
    pl.setAttribute("stroke", color);
    pl.setAttribute("stroke-width", "2");
    pl.setAttribute("stroke-linejoin", "round");
    svg.append(pl);
  });
}

export function render(root) {
  let rangeIdx = 3; // 30 days
  let sliceBy = "role";
  let busy = false;

  const rangeBox = h("div");
  const sliceBox = h("label", { class: "row sub" }, "Slice by ");
  const tiles = h("div", { class: "kpis" });
  const legend = h("div", { class: "legend" });
  const svg = h("svg", { role: "img", "aria-label": "Generation tokens per second over the selected range" });
  const err = h("p", { class: "error" });

  root.append(
    h(
      "div",
      { class: "row spread" },
      h("div", null, h("h1", { class: "h1" }, "Metrics"), h("div", { class: "sub" }, "Where to look when something feels off.")),
      h("div", { class: "row" }, rangeBox, sliceBox),
    ),
    tiles,
    h("section", { class: "card" }, h("h2", null, "Generation speed by ", h("span", { id: "glabel" }, sliceBy)), legend, svg, err),
  );

  function rebuildControls() {
    clear(rangeBox).append(rangePicker(rangeIdx, (i) => {
      rangeIdx = i;
      rebuildControls();
      load();
    }));
    clear(sliceBox);
    sliceBox.append("Slice by ", sliceSelect(sliceBy, (s) => {
      sliceBy = s;
      rebuildControls();
      load();
    }));
  }

  async function load() {
    if (busy) return;
    busy = true;
    err.replaceChildren();
    const to = Date.now() / 1000;
    const span = RANGES[rangeIdx][1];
    const from = span === null ? undefined : to - span;
    const q = { from, to, slice: sliceBy };
    let summary = null;
    let series = [];
    try {
      [summary, series] = await Promise.all([metricsSummary(q), metricsSeries({ metric: "generation_tps", ...q })]);
    } catch (e) {
      err.textContent = `Could not load metrics: ${e.message}`;
      busy = false;
      return;
    }
    clear(tiles);
    for (const [text, pick, unit] of TILES) {
      const v = summary ? pick(summary) : null;
      tiles.append(tile(text, fmt(v, unit)));
    }
    // legend: one swatch per slice value in the series rows, "unknown" for missing values
    const seen = new Set();
    for (const r of series) {
      const v = label(r[sliceBy]);
      if (!seen.has(v)) seen.add(v);
    }
    clear(legend);
    for (const [i, v] of [...seen].entries()) {
      legend.append(h("span", null, h("i", { style: { background: COLORS[i % COLORS.length] } }), `${v} `));
    }
    chart(svg, series, sliceBy);
    busy = false;
  }

  rebuildControls();
  load();
}
