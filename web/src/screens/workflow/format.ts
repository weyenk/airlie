import type { FlowJob } from "@/api/workflows";

/** 3, 217 and 3700 seconds as "3 s", "3 min 37 s" and "1 h 1 min". */
export function span(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "";
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}

/** The short form a box shows: "28 s", "2 min", "1 h 1 min". */
export function briefSpan(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "";
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return span(s);
}

export function count(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

const rate = (v: number | null): string | null => (v === null || v === undefined ? null : `${v} tok/s`);

/** The numbers of a job, as [label, value] pairs: only those it has. */
export function stats(job: FlowJob): [string, string][] {
  const t = job.tokens;
  const rows: [string, string | null][] = [["Attempts", String(job.attempts)]];
  if (job.fix_rounds) rows.push(["Fix rounds", String(job.fix_rounds)]);
  if (job.duration_s !== null && job.duration_s !== undefined) rows.push(["Duration", span(job.duration_s)]);
  if (t) {
    // a run's job carries only prompt and completion counts (the tree has no steps or speeds): what is missing is left out
    rows.push(["Steps", t.steps === undefined ? null : String(t.steps)]);
    rows.push(["Prompt tokens", t.prompt === undefined ? null : count(t.prompt)], ["Completion tokens", t.completion === undefined ? null : count(t.completion)]);
    rows.push(["Generation", rate(t.tps)], ["End to end", rate(t.effective_tps)], ["Prefill", rate(t.prefill_tps)]);
    if (t.model) rows.push(["Model", t.model]);
  }
  return rows.filter((r): r is [string, string] => Boolean(r[1]));
}
