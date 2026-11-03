import type { FlowStatus, FlowTag } from "@/api/workflows";

/** The box of a job in each status (the pipeline's look). A skipped job draws as a pending one. */
export const BOX: Record<FlowStatus, string> = {
  passed: "border-primary/45",
  running: "border-info-foreground",
  failed: "border-danger-foreground bg-danger",
  pending: "border-dashed text-muted-foreground",
  skipped: "border-dashed text-muted-foreground",
  stopped: "border-dashed border-warn-foreground",
};

/** The little round status mark. */
export const DOT: Record<FlowStatus, string> = {
  passed: "bg-ok-foreground",
  running: "bg-info-foreground shadow-[0_0_0_3px_var(--info-bg)]",
  failed: "bg-danger-foreground",
  pending: "bg-track",
  skipped: "bg-track",
  stopped: "bg-warn-foreground",
};

/** A square on a hover card: a status, or "warn", or a round "code-passed". */
export const MARK: Record<string, string> = {
  passed: "bg-ok-foreground",
  running: "bg-info-foreground",
  failed: "bg-danger-foreground",
  pending: "bg-track",
  skipped: "bg-track",
  stopped: "bg-warn-foreground",
  warn: "bg-warn-foreground",
  "code-passed": "rounded-full bg-ok-foreground",
};

/** A tag's tone. */
export const TAG: Record<FlowTag["tone"], string> = {
  warn: "bg-warn text-warn-foreground",
  info: "bg-info text-info-foreground",
  ok: "bg-ok text-ok-foreground",
  danger: "bg-danger text-danger-foreground",
  chip: "bg-chip text-chip-foreground",
};
