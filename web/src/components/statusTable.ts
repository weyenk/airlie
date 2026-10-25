// One table for every status chip: the words and the colour family of each value, per kind.
// Families map to the look's badge colours (bg-ok, bg-info, bg-warn, bg-danger, bg-violet, bg-chip).
export type Family = "ok" | "info" | "warn" | "danger" | "violet" | "chip";
export type ChipKind = "board" | "stage" | "job" | "run";
export interface ChipSpec { label: string; family: Family; dot?: boolean }

export const STATUS_TABLE: Record<ChipKind, Record<string, ChipSpec>> = {
  board: {
    todo: { label: "To do", family: "chip" },
    approved: { label: "Approved", family: "info" },
    doing: { label: "Doing", family: "info", dot: true },
    review: { label: "Review", family: "warn" },
    blocked: { label: "Blocked", family: "danger" },
    done: { label: "Done", family: "ok" },
  },
  stage: {
    refining: { label: "refining", family: "violet" },
    planning: { label: "planning layers", family: "info", dot: true },
    "planning layers": { label: "planning layers", family: "info", dot: true },
    building: { label: "building", family: "info", dot: true },
    publishing: { label: "publishing", family: "info", dot: true },
    review: { label: "in review", family: "warn" },
    "in review": { label: "in review", family: "warn" },
    blocked: { label: "blocked", family: "danger" },
  },
  job: {
    passed: { label: "passed", family: "ok" },
    running: { label: "running", family: "info", dot: true },
    failed: { label: "failed", family: "danger" },
    pending: { label: "pending", family: "chip" },
    waiting: { label: "waiting", family: "warn" },
  },
  run: {
    studio: { label: "studio run", family: "violet" },
    single: { label: "single run", family: "chip" },
    teams: { label: "teams run", family: "chip" },
    fork: { label: "fork run", family: "chip" },
    story: { label: "story request", family: "chip" },
  },
};

// Written out so Tailwind sees every class.
export const FAMILY_CLASS: Record<Family, string> = {
  ok: "bg-ok text-ok-foreground",
  info: "bg-info text-info-foreground",
  warn: "bg-warn text-warn-foreground",
  danger: "bg-danger text-danger-foreground",
  violet: "bg-violet text-violet-foreground",
  chip: "bg-chip text-chip-foreground",
};

export function chipSpec(kind: ChipKind, value: string): ChipSpec {
  const table = STATUS_TABLE[kind];
  return Object.hasOwn(table, value) ? table[value] : { label: value, family: "chip" };
}
