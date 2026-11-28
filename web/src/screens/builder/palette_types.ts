// Palette tiles vs every kind the backend accepts (the four kinds on the four-node-kind model).
import { NODE_KINDS, type NodeKind } from "./graph";

/** Kinds shown on the builder palette. */
export const PALETTE_TYPES = ["WORK", "ROUND_ROBIN", "SUPERVISOR_WORKERS", "K_INDEPENDENT_TEAMS"] as const satisfies readonly NodeKind[];

const LABELS: Record<(typeof PALETTE_TYPES)[number], string> = {
  // the kind's wire name stays WORK (the YAML and the engine say so); the person sees "Task"
  WORK: "Task",
  ROUND_ROBIN: "Round-robin",
  SUPERVISOR_WORKERS: "Supervisor",
  K_INDEPENDENT_TEAMS: "K teams",
};

export const paletteLabel = (type: (typeof PALETTE_TYPES)[number]): string => LABELS[type];

/** What a person sees for a kind: the palette's word, and the bare wire name for anything the palette does not
 * paint (an old type a file still loads). The wire name stays what the YAML and the engine say. */
export const kindLabel = (kind: string): string =>
  (LABELS as Record<string, string | undefined>)[kind] ?? kind;

/** All kinds the backend accepts. */
export const ALL_KINDS = NODE_KINDS;
