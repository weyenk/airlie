// The look of each node kind on the canvas: WORK is a job box, ROUND_ROBIN a conversation box,
// SUPERVISOR_WORKERS a team box, K_INDEPENDENT_TEAMS a multi-team box.
import type { NodeKind } from "./graph";

export type Shape = "work";

export const SHAPE: Record<NodeKind, Shape> = {
  WORK: "work",
  ROUND_ROBIN: "work",
  SUPERVISOR_WORKERS: "work",
  K_INDEPENDENT_TEAMS: "work",
};

/** The box's own classes per shape (no status: a definition has not run). */
export const SHAPE_CLASS: Record<Shape, string> = {
  work: "rounded-[10px] border border-primary/45 bg-card px-[11px] py-[7px]",
};

export const shapeOf = (kind: string): Shape => SHAPE[kind as NodeKind] ?? "work";
