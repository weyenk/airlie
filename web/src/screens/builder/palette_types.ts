// Palette tiles vs every kind the backend accepts (the four kinds on the four-node-kind model).
import { NODE_KINDS, type NodeKind } from "./graph";

/** Kinds shown on the builder palette. */
export const PALETTE_TYPES = ["WORK", "ROUND_ROBIN", "SUPERVISOR_WORKERS", "K_INDEPENDENT_TEAMS"] as const satisfies readonly NodeKind[];

const LABELS: Record<(typeof PALETTE_TYPES)[number], string> = {
  WORK: "Work",
  ROUND_ROBIN: "Round-robin",
  SUPERVISOR_WORKERS: "Supervisor",
  K_INDEPENDENT_TEAMS: "K teams",
};

export const paletteLabel = (type: (typeof PALETTE_TYPES)[number]): string => LABELS[type];

/** All kinds the backend accepts. */
export const ALL_KINDS = NODE_KINDS;
