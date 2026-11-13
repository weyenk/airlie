// Palette tiles vs every type the backend accepts (including deprecated legacy model types on load).
import { NODE_TYPES, type NodeType } from "./graph";

/** Types shown on the builder palette (bundles are listed separately). */
export const PALETTE_TYPES = ["model", "team", "check", "gate"] as const satisfies readonly NodeType[];

const LABELS: Record<(typeof PALETTE_TYPES)[number], string> = {
  model: "Model",
  team: "Team (supervisor)",
  check: "check",
  gate: "gate",
};

export const paletteLabel = (type: (typeof PALETTE_TYPES)[number]): string => LABELS[type];

/** Legacy `type` values rewritten to `model` + `protocol` on save (airlie.flow.upgrade.upgrade_topology_types). */
export const LEGACY_PROTOCOL_TYPES = NODE_TYPES.filter((t) => ["ask", "conversation", "write", "meeting"].includes(t));
