import type { Level, Status } from "@/api/board";

/** The board's columns, left to right. */
export const STATUSES: Status[] = ["todo", "approved", "doing", "review", "blocked", "done"];

/** The Type filter's choices: the four levels the board model has (the work types beyond them wait for the
 * work-types design). */
export const TYPE_FILTERS: { value: "all" | Level; label: string }[] = [
  { value: "all", label: "All" },
  { value: "initiative", label: "Initiative" },
  { value: "epic", label: "Epic" },
  { value: "story", label: "Story" },
  { value: "subtask", label: "Subtask" },
];
