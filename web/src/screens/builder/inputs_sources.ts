// What an input can come from, and where each kind's own candidates come from. Pure: no Vue, so the rules tests
// fast. The graph's inputs and a node's inputs stay plain names — a source says where a name's value comes from,
// not a second field to fill.

/** A box may read what the workflow was handed or an upstream box wrote (both named by the graph), and what
 * external source of value it needs — a repository checkout, an API, a database. */
export type SourceId = "workflow" | "box" | "repo" | "api" | "database" | "file" | "literal" | "unlisted";

export type SourceDef = {
  id: SourceId;
  /** What the picker says. */
  label: string;
  /** What its rows say when the box already reads something of this kind that nothing here offers. */
  note: string;
  /** A free text names the value instead of a second dropdown picking one. */
  free?: boolean;
  /** Only a box running a shell command can read this kind (a prompt cannot fetch a URL itself). */
  runOnly?: boolean;
};

export const SOURCES: readonly SourceDef[] = [
  { id: "workflow", label: "Workflow input", note: "named by the workflow's own inputs" },
  { id: "box", label: "Another box", note: "an output of a box upstream" },
  { id: "repo", label: "Repository", note: "a repository airlie has for you" },
  { id: "api", label: "API", note: "a URL or endpoint the box calls itself", free: true, runOnly: true },
  { id: "database", label: "Database", note: "a connection or DSN the box queries", free: true, runOnly: true },
  { id: "file", label: "File", note: "a path the box reads", free: true },
  { id: "literal", label: "Constant", note: "a value written into the box", free: true },
];

export const sourceOf = (id: string): SourceDef | undefined => SOURCES.find((s) => s.id === id);

/** An unknown name's placeholder in the first dropdown: it is not a kind to pick. */
export const UNLISTED: SourceId = "unlisted";

/** The sources a box may add: a prompt box gets the kinds named by the graph, a shell box those plus the kinds it
 * fetches itself. */
export const sourcesFor = (isRunBox: boolean): SourceDef[] => SOURCES.filter((s) => !s.runOnly || isRunBox);

/** The kinds an input already listed but unknown to the graph may be: any the box may read at all. A name outlives
 * what wrote it (a loop's `feedback.<box>` waits for a line, a workflow input can be handed over without being
 * declared), and a value typed here is unknown to the graph by nature. */
export const unlistedKinds = (isRunBox: boolean): SourceDef[] => sourcesFor(isRunBox);

/** The kinds whose value is text the box reads itself, not a name the graph could bring: an input of one of these
 * can never be carried by a line. `sourceOf(id)?.free` is not enough — a repository is picked from a list yet is
 * still a name the graph may not know. */
export const VALUE: readonly SourceId[] = ["api", "database", "file", "literal"];

/** An external value's text: what it names (no kind of value has a form of its own — the YAML holds the name, and
 * `values` holds what a previous edit typed for it, a box input being renamable behind our back). */
export const valueText = (name: string, values: Record<string, string>): string => values[name] ?? name;

/** A free-text value as the box's input name: a constant keeps the YAML the person typed (a list or a number
 * included), every other kind is the text itself. */
export function inputValue(id: SourceId, raw: string): string | null {
  const text = raw.trim();
  if (!text) return null;
  return id === "literal" ? text : text.replace(/\s+/g, " ");
}

/** Whether an input is one a wire could never bring: a typed value, the only kind a line cannot carry. A name of a
 * NAMED kind stays a name whoever wrote it in, so a line can still be drawn to it. */
export const isExternalValue = (id: SourceId): boolean => VALUE.includes(id);
