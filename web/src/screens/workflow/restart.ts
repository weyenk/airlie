// "Restart from here" on a story's pipeline: which step restarts the story from which point (airlie.story.restart's),
// and what the boxes' hover cards and the phone's sheet reach the screen's confirm dialog through.
import type { ComputedRef, InjectionKey } from "vue";
import type { FlowNode } from "@/api/workflows";

/** The steps before the layer plan: restarting from any of them is starting the story from scratch. */
const FROM_SCRATCH = new Set(["intake", "base", "gate", "survey", "refine"]);
const LATER = new Set(["layers", "amigos", "packages"]);
const LAYER = /^layer-\d\d$/;

/** The point a step restarts the story from, or null: a step it has not reached, or one with no restart (the planned
 * milestone, publish, review). */
export function restartPoint(node: FlowNode): string | null {
  if (node.status === "pending") return null;
  if (FROM_SCRATCH.has(node.id)) return "refine";
  if (LATER.has(node.id) || LAYER.test(node.id)) return node.id;
  return null;
}

/** What a restart from `point` keeps, in words for the confirm dialog. */
export function restartKeeps(point: string): string {
  if (point === "refine") return "Nothing: the story is refined again from the card as it reads now.";
  if (point === "layers") return "The refined story.";
  if (point === "amigos") return "The refined story and its layer plan.";
  if (point === "packages" || point === "layer-01") return "The agreed plan: the story, its layers and their proof.";
  const below = String(Number(point.slice("layer-".length)) - 1).padStart(2, "0");
  return `The agreed plan and layers 01 to ${below}, replayed on the latest main and checked by the story's gate there.`;
}

export type RestartContext = {
  /** The point the step `nodeId` restarts the story from, or null for none. */
  pointOf: (nodeId: string) => string | null;
  /** The story is running: a restart waits until it stops. */
  running: ComputedRef<boolean>;
  /** Ask to restart from the step `nodeId` (the screen's confirm dialog). */
  ask: (nodeId: string) => void;
};

export const RESTART: InjectionKey<RestartContext> = Symbol("restart");
