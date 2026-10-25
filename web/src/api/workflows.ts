// The Workflows routes (`/v1/airlie`): stories, what is running, the flow of a story or a run and its jobs, and
// starting and cancelling jobs. Types follow the server's rows (airlie.serve.listing, running, flow, run_flow).

import { queryString, request } from "./index";

export const STAGES = ["new", "intake", "refine", "layers", "build", "publish", "review"] as const;
export type Stage = (typeof STAGES)[number];

/** A story as the list shows it. `stage` is the last stage finished (null: no state yet); `blocked` is why it is
 * stuck, or null. `last_activity` is epoch seconds. */
export type StoryRow = {
  key: string;
  title: string | null;
  repo: string | null;
  stage: string | null;
  blocked: string | null;
  last_activity: number;
};

export type StoriesPage = { stories: StoryRow[]; total: number; page: number; size: number | null };

export type StoryQuery = {
  page?: number;
  /** Without a size the page is every row. */
  size?: number;
  stage?: Stage;
  repo?: string;
  blocked?: boolean;
  q?: string;
  sort?: "activity" | "key" | "stage";
  active_from?: number;
  active_to?: number;
};

/** A story or a standalone run in progress, whoever started it. `progress` is 0 to 1; null is an indeterminate bar. */
export type RunningRow = {
  kind: "story" | "run";
  key: string;
  title: string | null;
  topology: string | null;
  progress: number | null;
  last_activity: number;
};

/** A job this server started (they do not survive a restart). */
export type JobRow = {
  id: string;
  kind: "story" | "run" | "plan";
  state: "running" | "done" | "failed" | "cancelled";
  started: number;
  error?: string;
};

export type RunRow = {
  id: string;
  goal: string | null;
  repo: string | null;
  kind: "run" | "plan";
  started: number | null;
  ok: boolean | null;
  wall_ms: number | null;
};

export type JobStart = { job_id: string; kind: "story" | "run" | "plan" };

export type FlowStatus = "pending" | "running" | "passed" | "failed" | "skipped";

export type Tokens = {
  steps: number;
  prompt: number;
  completion: number;
  gen_s: number;
  prefill_s: number;
  tool_s: number;
  tps: number | null;
  effective_tps: number | null;
  prefill_tps: number | null;
  role: string | null;
  model: string | null;
};

export type FlowJob = {
  id: string;
  label: string;
  role: string | null;
  status: FlowStatus;
  attempts: number;
  fix_rounds: number;
  retries: { kind: string; detail: string }[];
  studio_run: string | null;
  started: number | null;
  ended: number | null;
  duration_s: number | null;
  tokens: Tokens | null;
  /** A run's job: the agent that won the fork. */
  won?: boolean;
};

export type FlowNode = {
  id: string;
  label: string;
  kind: "stage" | "layer";
  status: FlowStatus;
  jobs: FlowJob[];
  /** A layer's own fields. */
  title?: string | null;
  scope?: string | null;
  acceptance?: string[];
  attempts?: number;
};

/** One acceptance criterion of a story, as the refine stage wrote it. */
export type Criterion = { id: string; given: string; when: string; then: string };

/** A story's flow, or a run's (the extra fields of a run are `kind: "run"`, `topology`, `mode`, `ok`, `progress`,
 * and `job` and `plan`: this server's job state for it, and the plan a finished plan job returned). */
export type Flow = {
  key: string;
  title: string | null;
  summary: string | null;
  stage: string | null;
  blocked: string | null;
  acceptance: Criterion[];
  nodes: FlowNode[];
  kind?: "run";
  topology?: string | null;
  mode?: string | null;
  ok?: boolean | null;
  progress?: number | null;
  job?: string | null;
  plan?: Record<string, unknown> | null;
};

export type FlowStep = {
  attempt: number;
  n: number;
  tool: string | null;
  arguments: unknown;
  ok: boolean | null;
  observation: string;
  assistant_text: string | null;
  reasoning: string | null;
};

export type JobDetail = {
  node: string;
  job: FlowJob;
  /** What the job was opened with: the chat messages of its first prompt (null when none was recorded). */
  input: { messages: { role: string; content: string | null }[] } | null;
  input_note: string | null;
  output: { final_text: string } & Record<string, unknown>;
  steps: FlowStep[];
};

export function listStories(query: StoryQuery = {}): Promise<StoriesPage> {
  return request<StoriesPage>(`/v1/airlie/stories${queryString({ ...query })}`);
}

export async function listRunning(): Promise<RunningRow[]> {
  return (await request<{ running: RunningRow[] }>("/v1/airlie/running")).running;
}

export async function listJobs(): Promise<JobRow[]> {
  return (await request<{ jobs: JobRow[] }>("/v1/airlie/jobs")).jobs;
}

export async function listRuns(limit = 50): Promise<RunRow[]> {
  return (await request<{ runs: RunRow[] }>(`/v1/airlie/runs${queryString({ limit })}`)).runs;
}

const seg = encodeURIComponent;

export function storyFlow(key: string): Promise<Flow> {
  return request<Flow>(`/v1/airlie/stories/${seg(key)}/flow`);
}

export function storyJob(key: string, node: string, job: string): Promise<JobDetail> {
  return request<JobDetail>(`/v1/airlie/stories/${seg(key)}/flow/${seg(node)}/${seg(job)}`);
}

export function runFlow(id: string): Promise<Flow> {
  return request<Flow>(`/v1/airlie/runs/${seg(id)}/flow`);
}

export function runJob(id: string, node: string, job: string): Promise<JobDetail> {
  return request<JobDetail>(`/v1/airlie/runs/${seg(id)}/flow/${seg(node)}/${seg(job)}`);
}

/** The repositories a written request may name (`owner/name`). */
export async function repoNames(): Promise<string[]> {
  return (await request<{ repos: string[] }>("/v1/airlie/repos/names")).repos;
}

/** Start a story from a board card, or from a written request (and the repository it is for). */
export function startStory(body: { card: string } | { text: string; repo?: string | null }): Promise<JobStart> {
  return request<JobStart>("/v1/airlie/stories", { method: "POST", body });
}

/** Start a studio run from an approved plan. */
export function startRun(body: {
  goal: string;
  repo: string;
  plan?: Record<string, unknown> | null;
  test_cmd?: string | null;
  base?: string | null;
  images?: string[];
  synthesise?: boolean;
}): Promise<JobStart> {
  return request<JobStart>("/v1/airlie/runs", { method: "POST", body });
}

export function cancelJob(id: string): Promise<{ job_id: string; cancelled: boolean }> {
  return request(`/v1/airlie/jobs/${seg(id)}`, { method: "DELETE" });
}
