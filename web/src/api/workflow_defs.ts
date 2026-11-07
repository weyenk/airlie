// The workflow definitions (`/v1/workflows`): the files and drafts a card may name, the builder's catalog, a draft's
// save and delete, validation and publish. Types follow airlie.serve.workflows.

import { request } from "./index";

/** A workflow a card may name: a file in the repo or a draft, the type it declares (null: a bundle), its problems,
 * and its open PR when it has one. */
export type WorkflowDef = {
  name: string;
  type: string | null;
  source: "file" | "draft";
  problems: string[];
  pr: { number: number; url: string } | null;
  bundle?: boolean;
  /** A draft named as a file: that file's edit waiting for its PR (or a merge the serve has not loaded yet). The
   * file runs; this draft never does. */
  superseded?: boolean;
};

/** One definition whole: its YAML, where its boxes sit and the text of the prompt files it names. */
export type WorkflowDetail = {
  name: string;
  type: string | null;
  source: "file" | "draft";
  yaml: string;
  layout: Record<string, { x: number; y: number }>;
  problems: string[];
  prompts: Record<string, string>;
  pr: { number: number; url: string } | null;
  /** A draft named as a file: an edit of that file, published as a change to it; the file runs meanwhile. */
  edits_file?: boolean;
};

/** What the builder's palette and forms may pick from. */
export type Catalog = {
  roles: string[];
  types: string[];
  prompts: string[];
  parsers: string[];
  checks: string[];
  predicates: string[];
  afters: string[];
  turns: string[];
  meetings: string[];
  prompt_files: string[];
};

export type DraftBody = { type: string | null; yaml: string; prompts: Record<string, string> };

const seg = encodeURIComponent;

export async function listWorkflowDefs(): Promise<WorkflowDef[]> {
  return (await request<{ workflows: WorkflowDef[] }>("/v1/workflows")).workflows;
}

export function registryCatalog(): Promise<Catalog> {
  return request<Catalog>("/v1/workflows/catalog");
}

export function getWorkflowDef(name: string): Promise<WorkflowDetail> {
  return request<WorkflowDetail>(`/v1/workflows/${seg(name)}`);
}

/** Save a draft (422: the name is reserved, or the YAML does not parse). A file's name saves an edit of that file. */
export function saveDraft(name: string, body: DraftBody): Promise<WorkflowDetail> {
  return request<WorkflowDetail>(`/v1/workflows/drafts/${seg(name)}`, { method: "PUT", body });
}

export function deleteDraft(name: string): Promise<void> {
  return request<void>(`/v1/workflows/drafts/${seg(name)}`, { method: "DELETE" });
}

/** The problems of a workflow's YAML (and the prompt files it brings), none when it validates. */
export async function validateWorkflow(yaml: string, prompts?: Record<string, string>): Promise<string[]> {
  const body = prompts === undefined ? { yaml } : { yaml, prompts };
  return (await request<{ problems: string[] }>("/v1/workflows/validate", { method: "POST", body })).problems;
}

/** Open the workflow's PR (422: it has problems; 409: nothing to publish; 502: GitHub refused). */
export function publishWorkflow(name: string): Promise<{ number: number; url: string }> {
  return request<{ number: number; url: string }>(`/v1/workflows/${seg(name)}/publish`, { method: "POST" });
}
