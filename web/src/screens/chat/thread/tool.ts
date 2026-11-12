// What a tool part says for a person: its name, the one-line summary, its state in words, its output as text.

import type { ToolUIPart } from "ai";
import { INTERRUPTED } from "@/stores/turns";

/** A tool part as the thread holds it (the SDK's type names every tool; these are the fields this screen reads). */
export type Call = {
  type: string;
  toolCallId: string;
  state: string;
  input?: unknown;
  output?: unknown;
  errorText?: string;
};

export const callOf = (part: ToolUIPart): Call => part as unknown as Call;

export const toolName = (part: ToolUIPart): string => callOf(part).type.slice("tool-".length);

const OPEN = new Set(["input-streaming", "input-available", "approval-requested", "approval-responded"]);

export type Phase = "running" | "waiting" | "done" | "failed" | "denied" | "interrupted";

/** The call's state in one word: an open call of a cut message, or one the store marked, is interrupted. */
export function phaseOf(part: ToolUIPart, interrupted: boolean): Phase {
  const c = callOf(part);
  if (c.state === "output-error") return c.errorText === INTERRUPTED ? "interrupted" : "failed";
  if (OPEN.has(c.state) && interrupted) return "interrupted";
  switch (c.state) {
    case "output-available":
      return "done";
    case "output-denied":
      return "denied";
    case "approval-requested":
      return "waiting";
    default:
      return "running";
  }
}

export const PHASE_TEXT: Record<Phase, string> = {
  running: "running",
  waiting: "waiting for approval",
  done: "done",
  failed: "failed",
  denied: "denied",
  interrupted: "interrupted",
};

export const inputOf = (part: ToolUIPart): Record<string, unknown> => {
  const input = callOf(part).input;
  return input && typeof input === "object" && !Array.isArray(input) ? (input as Record<string, unknown>) : {};
};

/** A value as text: a string as it is, anything else as JSON. */
export const textOfValue = (v: unknown): string => {
  if (typeof v === "string") return v;
  if (v === undefined) return "";
  return JSON.stringify(v, null, 2) ?? "";
};

/** What the call produced: its output, or its error. */
export function outputText(part: ToolUIPart): string {
  const c = callOf(part);
  return c.state === "output-error" ? (c.errorText ?? "") : textOfValue(c.output);
}

/** The row's one line: the path for a file tool, the command for `shell`, else the first line of the output. */
export function summaryOf(part: ToolUIPart): string {
  const input = inputOf(part);
  if (typeof input.path === "string" && input.path) return input.path;
  if (Array.isArray(input.paths) && input.paths.length) return input.paths.join(", ");
  if (typeof input.command === "string") return input.command;
  const c = callOf(part);
  return c.state === "output-available" ? (outputText(part).split("\n").find((l) => l.trim()) ?? "") : "";
}

export type DiffSource = { path: string; old?: string; new: string };

/** The diff an `edit_file` or `write_file` call stands for, from its input. */
export function diffOf(part: ToolUIPart): DiffSource | null {
  const input = inputOf(part);
  const name = toolName(part);
  if (typeof input.path !== "string") return null;
  if (name === "edit_file" && typeof input.new === "string") return { path: input.path, old: String(input.old ?? ""), new: input.new };
  if (name === "write_file" && typeof input.content === "string") return { path: input.path, new: input.content };
  return null;
}

/** The lines of a text as a person counts them (a closing newline starts no line). */
export const linesOf = (text: string): string[] => {
  if (!text) return [];
  const lines = text.split("\n");
  if (lines.at(-1) === "") lines.pop();
  return lines;
};
