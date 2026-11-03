// Test fixtures: flows, jobs and job details as the server sends them.
import type { Flow, FlowExit, FlowJob, FlowNode, FlowNow, FlowStep, JobDetail, LaneRow } from "@/api/workflows";

export const job = (over: Partial<FlowJob> = {}): FlowJob => ({
  id: "build",
  label: "Build",
  role: "coder",
  status: "passed",
  attempts: 1,
  fix_rounds: 0,
  retries: [],
  studio_run: "run-1",
  started: 1,
  ended: 2,
  duration_s: 125,
  tokens: null,
  ...over,
});

export const stage = (id: string, label: string, status: FlowNode["status"] = "passed", over: Partial<FlowJob> = {}): FlowNode => ({
  id,
  label,
  kind: "stage",
  status,
  // the server leaves a skipped stage (no survey) without jobs
  jobs: status === "skipped" ? [] : [job({ id, label, role: null, status, ...over })],
});

export const layer = (nn: string, title: string, status: FlowNode["status"], states: [FlowJob["status"], FlowJob["status"], FlowJob["status"]], over: Partial<FlowNode> = {}): FlowNode => ({
  id: `layer-${nn}`,
  label: `Layer ${nn}`,
  kind: "layer",
  status,
  title,
  acceptance: ["AC1"],
  attempts: 1,
  jobs: [
    job({ id: "tests", label: "Tests", role: "tester", status: states[0] }),
    job({ id: "checks", label: "Test checks", role: null, status: states[1], duration_s: null }),
    job({ id: "build", label: "Build", role: "coder", status: states[2] }),
  ],
  ...over,
});

export const flow = (nodes: FlowNode[], over: Partial<Flow> = {}): Flow => ({
  key: "AIR-17",
  title: "Board card modal",
  summary: null,
  stage: "layers",
  blocked: null,
  acceptance: [],
  nodes,
  ...over,
});

export const step = (over: Partial<FlowStep> = {}): FlowStep => ({
  attempt: 0,
  n: 1,
  tool: "read_file",
  arguments: "{}",
  ok: true,
  observation: "1: x",
  assistant_text: null,
  reasoning: null,
  ...over,
});

export const detail = (over: Partial<JobDetail> = {}): JobDetail => ({
  node: "layer-01",
  job: job(),
  input: null,
  input_note: "opening prompt not recorded",
  output: { final_text: "" },
  steps: [],
  ...over,
});

/** A node of the plan lane; `shape` as the server sends it. */
export const planStage = (id: string, label: string, status: FlowNode["status"] = "passed", over: Partial<FlowNode> = {}, shape: FlowJob["shape"] = "model"): FlowNode => ({
  ...stage(id, label, status, { shape }),
  lane: "plan",
  ...over,
});

/** A layer with rows (try cycles and dividers), as the build lane sends it. */
export const lane = (nn: string, title: string, status: FlowNode["status"], rows: LaneRow[], over: Partial<FlowNode> = {}): FlowNode => {
  const last = [...rows].reverse().find((r) => r.kind === "cycle");
  return { ...layer(nn, title, status, ["pending", "pending", "pending"]), lane: "build", rows, jobs: last?.kind === "cycle" ? last.jobs : [], ...over };
};

export const cycle = (id: string, label: string, status: FlowNode["status"], prefix = ""): LaneRow => ({
  kind: "cycle",
  id,
  label,
  status,
  jobs: [
    job({ id: `${prefix}package`, label: "Package", role: "architect", status, shape: "model" }),
    job({ id: `${prefix}checks`, label: "Checks", role: null, status, shape: "code" }),
    job({ id: `${prefix}build`, label: "Build", role: "coder", status, shape: "model" }),
  ],
});

export const now = (over: Partial<FlowNow> = {}): FlowNow => ({
  status: "running",
  path: ["Build lane", "Draft packages"],
  node: "packages",
  job: "packages",
  inner: "L03 · package",
  role: "architect",
  steps: 6,
  started: null,
  last_event: null,
  text: null,
  ...over,
});

export const exitRow = (over: Partial<FlowExit> = {}): FlowExit => ({
  kind: "blocked",
  where: "L02 build 2 of 2",
  times: 3,
  at: null,
  reason: "tests still red",
  on: null,
  jobs: [job({ id: "diagnose", label: "Diagnose", role: "muse", shape: "model" }), job({ id: "fix", label: "Fix card", role: null, shape: "code" })],
  ...over,
});
