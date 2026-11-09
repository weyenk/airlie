// A prompt file a draft carries (airlie.flow.prompt_files; the hub's format, airlie.prompts.hub): YAML front
// matter between `---` lines (`kind: rule`, `roles`, `fields`, `why`), then a body whose `{field}` placeholders
// the node's inputs fill (`{{` and `}}` write a brace). Pure: no Vue.
import { Document, parse, visit } from "yaml";

export type PromptFile = { roles: string[]; fields: string[]; why: string; body: string };

export const PROMPT_ID = /^[a-z0-9]+(-[a-z0-9]+)*$/;

/** The file's text, as the hub would read it. */
export function promptFileText(file: PromptFile): string {
  const meta: Record<string, unknown> = { kind: "rule", roles: file.roles };
  if (file.fields.length) meta.fields = file.fields;
  meta.why = file.why.endsWith("\n") ? file.why : `${file.why}\n`;
  const doc = new Document(meta);
  visit(doc, {
    Seq(_, seq) {
      seq.flow = true;
    },
  });
  return `---\n${doc.toString({ lineWidth: 0, flowCollectionPadding: false, blockQuote: "literal" })}---\n${file.body}`;
}

/** The placeholders a body fills. */
function placeholders(body: string): Set<string> {
  const plain = body.replaceAll("{{", "").replaceAll("}}", "");
  return new Set([...plain.matchAll(/\{([^{}]+)\}/g)].map((m) => m[1]));
}

/** What stops the hub reading the file, as the hub's own checks would say it; none when it reads. `hub` are
 * the hub's rule ids, which a draft may not take. */
export function promptProblems(file: PromptFile & { id: string }, hub: string[]): string[] {
  const out: string[] = [];
  if (!PROMPT_ID.test(file.id)) out.push("the id must be kebab-case: lower-case words joined by -");
  else if (hub.includes(file.id)) out.push(`the hub already has a prompt file ${file.id}`);
  if (!file.roles.length) out.push("pick at least one role");
  if (!file.why.trim()) out.push("why is required: what the prompt is for, and the incidents behind it");
  const used = placeholders(file.body);
  for (const n of [...used].sort()) if (!file.fields.includes(n)) out.push(`{${n}} is not one of the fields`);
  for (const n of file.fields) if (!used.has(n)) out.push(`field ${n} is never used in the body`);
  return out;
}

/** The `fields` a prompt file's front matter lists, or undefined when the text has no front matter that reads. */
export function fieldsOf(text: string): string[] | undefined {
  const m = /^---\n([\s\S]*?)\n---\n/.exec(text);
  if (!m) return undefined;
  try {
    const meta: unknown = parse(m[1]);
    if (typeof meta !== "object" || meta === null) return undefined;
    const fields = (meta as { fields?: unknown }).fields;
    return Array.isArray(fields) ? fields.filter((f): f is string => typeof f === "string") : [];
  } catch {
    return undefined;
  }
}

/** The fields of the draft's prompt files that read (id -> fields), for `renameOutput` to know which name what. */
export function promptFieldsOf(prompts: Record<string, string>): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const [id, text] of Object.entries(prompts)) {
    const fields = fieldsOf(text);
    if (fields) out[id] = fields;
  }
  return out;
}
