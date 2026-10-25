// Test fixtures: flows, jobs and job details as the server sends them.
import type { Flow, FlowJob, FlowNode, FlowStep, JobDetail } from "@/api/workflows";

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
