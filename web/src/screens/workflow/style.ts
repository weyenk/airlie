import type { FlowStatus } from "@/api/workflows";

/** The box of a job in each status (the pipeline's look). A skipped job draws as a pending one. */
export const BOX: Record<FlowStatus, string> = {
  passed: "border-primary/45",
  running: "border-info-foreground",
  failed: "border-danger-foreground bg-danger",
  pending: "border-dashed text-muted-foreground",
  skipped: "border-dashed text-muted-foreground",
};

/** The little round status mark. */
export const DOT: Record<FlowStatus, string> = {
  passed: "bg-ok-foreground",
  running: "bg-info-foreground shadow-[0_0_0_3px_var(--info-bg)]",
  failed: "bg-danger-foreground",
  pending: "bg-track",
  skipped: "bg-track",
};
