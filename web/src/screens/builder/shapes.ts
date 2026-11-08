// The look of each node type, in the pipeline screen's shapes (docs/superpowers/specs/2026-11-02-pipeline-matches-
// diagram-design.md): a model step is JobBox's, a check CodeBox's, a gate a dashed code box, a parallel node the
// "same time" bracket, an each node a container holding its bundle.
import type { NodeType } from "./graph";

export type Shape = "model" | "code" | "gate" | "parallel" | "each";

export const SHAPE: Record<NodeType, Shape> = {
  ask: "model",
  conversation: "model",
  write: "model",
  meeting: "model",
  check: "code",
  gate: "gate",
  parallel: "parallel",
  each: "each",
};

/** The box's own classes per shape (no status: a definition has not run). */
export const SHAPE_CLASS: Record<Shape, string> = {
  model: "rounded-[10px] border border-primary/45 bg-card px-[11px] py-[7px]",
  code: "rounded-[4px] border border-primary/45 bg-[var(--code)] px-2 py-1.5 font-mono",
  gate: "rounded-[4px] border border-dashed border-primary/70 bg-[var(--code)] px-2 py-1.5 font-mono",
  parallel: "rounded-xl border border-dashed border-border bg-card/60 px-2 py-1",
  each: "rounded-xl border-2 border-border bg-muted/60 px-2.5 py-1.5",
};

export const shapeOf = (type: string): Shape => SHAPE[type as NodeType] ?? "model";
