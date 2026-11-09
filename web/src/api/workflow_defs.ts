// The workflow definitions (`/v1/workflows`): the files and drafts a card may name, the builder's catalog, a draft's
// save and delete, validation and publish. Types follow airlie.serve.workflows.

import { ApiError, request } from "./index";

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
  /** The workflows whose bundle nodes run this one. */
  used_by: string[];
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

/** A check the engine offers (`GET /v1/workflows/catalog`): what it reads, what it writes and in which format. */
export type CheckDef = {
  name: string;
  title: string;
  inputs: string[];
  outputs: string[];
  formats: Record<string, string>;
  transient: string[];
  every_run: boolean;
  args: string[];
};

/** A meeting the engine knows: the roles that sit in it and what it reads. */
export type MeetingDef = { roles: string[]; inputs: string[] };

/** What the builder's palette and forms may pick from. */
export type Catalog = {
  roles: string[];
  types: string[];
  formats: string[];
  kinds: string[];
  checks: CheckDef[];
  meetings: MeetingDef[];
  prompt_files: string[];
};

export const EMPTY_CATALOG: Catalog = { roles: [], types: [], formats: [], kinds: [], checks: [], meetings: [], prompt_files: [] };

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

/** The story's workflow moved to the one its card names (409: the story is running; 422: it cannot adopt it). */
export function adoptWorkflow(key: string): Promise<{ key: string; name: string; from: string | null; to: string | null }> {
  return request(`/v1/airlie/stories/${seg(key)}/adopt-workflow`, { method: "POST" });
}

/** The workflows a 409 names as using the draft (its detail is `{used_by: [...]}`, which arrives as JSON text);
 * null when the error is not that. */
export function usedBy(e: ApiError): string[] | null {
  if (e.status !== 409) return null;
  try {
    const found = (JSON.parse(e.message) as { used_by?: unknown } | null)?.used_by;
    if (Array.isArray(found) && found.every((u) => typeof u === "string")) return found;
  } catch {
    // not JSON: some other 409
  }
  return null;
}
