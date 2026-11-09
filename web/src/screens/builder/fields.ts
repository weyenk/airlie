// A structured output's field list as the builder edits it (airlie.flow.outputs): the kind grammar
// (`parse_kind`), the sentence the `output-format` rule says (`describe`), and an output's declaration read bare
// or full. Pure: no Vue.
import type { OutputSpec } from "./graph";

/** One parsed field kind: text, number, bool, a list of a kind, or one of some options; `optional` is the
 * trailing `?`. */
export type Kind = { name: "text" | "number" | "bool" | "list" | "one_of"; optional: boolean; item?: Kind; options?: string[] };

const SCALARS = ["text", "number", "bool"] as const;

/** The kind a field's text names, or an Error saying it is not in the grammar, worded as the server words it. */
export function parseKind(text: string): Kind | Error {
  let body = text.trim();
  const optional = body.endsWith("?");
  if (optional) body = body.slice(0, -1).trim();
  for (const s of SCALARS) if (body === s) return { name: s, optional };
  if (body.startsWith("list of ")) {
    const item = parseKind(body.slice("list of ".length));
    if (item instanceof Error) return new Error(`invalid field kind '${text}': a list needs a kind of item`);
    return { name: "list", optional, item };
  }
  const choosing = body.includes("|") || body.startsWith("one of ");
  const names = choosing ? (body.startsWith("one of ") ? body.slice("one of ".length) : body).split("|").map((o) => o.trim()) : [];
  if (names.length && names.every(Boolean)) return { name: "one_of", optional, options: names };
  return new Error(`invalid field kind '${text}': use text, number, bool, list of <kind>, or choices like 'a | b'`);
}

/** A kind as the server writes it in a sentence (no `?`: optional is said apart). */
export function kindText(kind: Kind): string {
  if (kind.name === "list" && kind.item) return `list of ${kindText(kind.item)}`;
  return kind.name === "one_of" ? `one of ${(kind.options ?? []).join(" | ")}` : kind.name;
}

/** The hub rule `output-format`'s body (src/airlie/prompts/rules/output-format.md; fields.test.ts holds it to
 * the file). */
export const OUTPUT_FORMAT_RULE = "Reply with one ```json block with fields: {fields}.";

/** The sentence the server's `describe` adds for a json output with these fields: the rule filled with each field
 * and its kind. Offered to the prompt by a button, never added on its own. A kind outside the grammar is written
 * as typed (validation names it). */
export function formatSentence(fields: Record<string, string>): string {
  const listing = Object.entries(fields)
    .map(([name, text]) => {
      const k = parseKind(text);
      if (k instanceof Error) return `${name} (${text.trim()})`;
      return `${name} (${kindText(k)}${k.optional ? ", optional" : ""})`;
    })
    .join(", ");
  return OUTPUT_FORMAT_RULE.replace("{fields}", () => listing); // a function: `$&` in a field is text
}

/** A fields text, one `name: kind` per line (blank lines skipped), as the fields it names and what is wrong. */
export function parseFieldLines(text: string): { fields: Record<string, string>; errors: string[] } {
  const fields: Record<string, string> = {};
  const errors: string[] = [];
  text.split("\n").forEach((raw, i) => {
    const line = raw.trim();
    if (!line) return;
    const at = line.indexOf(":");
    const name = at < 0 ? "" : line.slice(0, at).trim();
    const kind = at < 0 ? "" : line.slice(at + 1).trim();
    if (!name || !kind) {
      errors.push(`line ${i + 1}: write name: kind`);
      return;
    }
    if (name in fields) {
      errors.push(`${name} is listed twice`);
      return;
    }
    const k = parseKind(kind);
    if (k instanceof Error) errors.push(`${name}: ${k.message}`);
    fields[name] = kind;
  });
  return { fields, errors };
}

/** Fields as the text the editor shows: one `name: kind` per line. */
export const fieldLines = (fields: Record<string, string> | undefined): string =>
  Object.entries(fields ?? {})
    .map(([n, k]) => `${n}: ${k}`)
    .join("\n");

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** One output's declaration as a spec, whether the YAML wrote a bare format or the mapping. */
export function outputSpec(v: unknown): OutputSpec {
  if (typeof v === "string") return { format: v };
  if (!isRecord(v)) return { format: "text" };
  const spec: OutputSpec = { format: typeof v.format === "string" ? v.format : "text" };
  if (isRecord(v.fields)) spec.fields = Object.fromEntries(Object.entries(v.fields).map(([k, x]) => [k, String(x)]));
  if (v.transient === true) spec.transient = true;
  return spec;
}

/** A node's `outputs` field as specs, in order. */
export const outputSpecs = (v: unknown): Record<string, OutputSpec> => (isRecord(v) ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, outputSpec(x)])) : {});

/** A spec as the YAML should write it: the bare format when it says nothing more. */
export function specValue(spec: OutputSpec): string | OutputSpec {
  const fields = spec.fields && Object.keys(spec.fields).length ? spec.fields : undefined;
  if (!fields && !spec.transient) return spec.format;
  return { format: spec.format, ...(fields ? { fields } : {}), ...(spec.transient ? { transient: true } : {}) };
}
