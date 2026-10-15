// airlie's OpenCode front door: the studio (plan → approve → run) and the story pipeline, through `task serve`.
// `task opencode:install` copies this file to ~/.config/opencode/tool/, where @opencode-ai/plugin is installed;
// OpenCode names each export airlie_<export>: airlie_plan, airlie_run, airlie_status, airlie_cancel, airlie_story.
import { tool, type ToolContext } from "@opencode-ai/plugin"

const BASE = (process.env.AIRLIE_URL ?? "http://127.0.0.1:8090").replace(/\/+$/, "")
const cursors = new Map<string, number>()

async function call(method: string, path: string, body: unknown, signal?: AbortSignal): Promise<any> {
  const headers: Record<string, string> = { "content-type": "application/json" }
  const key = process.env.AIRLIE_SERVE_API_KEY
  if (key) headers.authorization = `Bearer ${key}`
  let res: Response
  let text: string
  try {
    res = await fetch(BASE + path, {
      method,
      headers,
      signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    text = await res.text()
  } catch (e) {
    if (signal?.aborted) throw new Error(`cancelled: ${method} ${path} was aborted`)
    throw new Error(`airlie is not reachable at ${BASE}; start it with \`task serve\` (${e})`)
  }
  if (!res.ok) throw new Error(`airlie ${method} ${path}: ${res.status} ${text.slice(0, 800)}`)
  return text ? JSON.parse(text) : {}
}

async function follow(jobId: string, wait: number, signal: AbortSignal): Promise<any> {
  const since = cursors.get(jobId) ?? 0
  const q = `?since=${since}&wait=${wait}`
  const r = await call("GET", `/v1/airlie/jobs/${encodeURIComponent(jobId)}${q}`, undefined, signal)
  cursors.set(jobId, r.next)
  return r
}

// OpenCode aborted the call (Esc). A job's id only comes back with the answer to the POST that starts it, so that
// POST is sent without the abort signal; the tool then stops the job here, best-effort (airlie gets 5 seconds), and
// reports the call as cancelled whether or not airlie confirmed it.
async function cancelled(what: string, jobId: string): Promise<Error> {
  const path = `/v1/airlie/jobs/${encodeURIComponent(jobId)}`
  const fate = await call("DELETE", path, undefined, AbortSignal.timeout(5000)).then(
    (r) => (r.cancelled ? "was stopped" : "had already ended"),
    () => "may still be running: airlie did not confirm that it stopped",
  )
  return new Error(`${what} cancelled: job ${jobId} ${fate}`)
}

// A test command the model chose runs in a shell on this machine: the person approves it like any bash command.
async function approve(cmd: string | undefined, ctx: ToolContext): Promise<void> {
  if (cmd) await ctx.ask({ permission: "bash", patterns: [cmd], always: [cmd], metadata: { command: cmd } })
}

function table(plan: any): string {
  const rows = (plan.subtasks ?? []).map((s: any) => {
    const deps = (s.depends_on ?? []).join(", ") || "-"
    return `| ${s.id} | ${s.role ?? "-"} | ${String(s.brief).replace(/\s+/g, " ")} | ${deps} |`
  })
  const assumptions = (plan.assumptions ?? []).map((a: string) => `- ${a}`)
  return [
    "| id | role | brief | depends on |",
    "| --- | --- | --- | --- |",
    ...rows,
    ...(assumptions.length ? ["", "Assumptions:", ...assumptions] : []),
  ].join("\n")
}

function line(e: any): string {
  const { ts, run_id, kind, ...rest } = e
  const fields = Object.entries(rest)
    .filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && v.length === 0))
    .map(([k, v]) => `${k}=${(typeof v === "string" ? v : JSON.stringify(v)).slice(0, 160)}`)
  return `- ${kind} ${fields.join(" ")}`
}

export const plan = tool({
  description:
    "Ask the airlie studio's chief for a plan (subtasks, roles, dependencies) for a goal in this repository. " +
    "Nothing is built. Show the plan to the person and wait for their approval before airlie_run.",
  args: {
    goal: tool.schema.string().describe("what to build or change, in the person's words"),
    test_cmd: tool.schema.string().optional().describe("command that runs this repository's tests"),
  },
  async execute(args, ctx) {
    const body = { goal: args.goal, repo: ctx.worktree, test_cmd: args.test_cmd }
    const { job_id } = await call("POST", "/v1/airlie/plans", body)
    let r: any
    try {
      r = await follow(job_id, 60, ctx.abort)
      while (r.state === "running") r = await follow(job_id, 60, ctx.abort)
    } catch (e) {
      if (ctx.abort.aborted) throw await cancelled("airlie_plan", job_id)
      throw e
    }
    if (r.state !== "done") throw new Error(`planning ${r.state}: ${r.error ?? ""}`)
    if (r.result.type === "answer") return `The chief answered without a plan:\n\n${r.result.answer}`
    return `${table(r.result.plan)}\n\nPlan JSON for airlie_run:\n${JSON.stringify(r.result.plan)}`
  },
})

export const run = tool({
  description:
    "Build in this repository with the airlie studio, on a new branch. Pass the plan the person approved (the " +
    "JSON from airlie_plan, edited as agreed); without a plan the chief plans and runs at once. Returns a job " +
    "id: follow it with airlie_status.",
  args: {
    goal: tool.schema.string().describe("the goal the plan was made for"),
    plan: tool.schema.string().optional().describe("the approved plan as JSON"),
    test_cmd: tool.schema.string().optional().describe("command that runs this repository's tests"),
  },
  async execute(args, ctx) {
    let plan: unknown
    if (args.plan) {
      try {
        plan = JSON.parse(args.plan)
      } catch (e) {
        throw new Error(`plan is not valid JSON (${e}); pass the JSON airlie_plan returned`)
      }
    }
    await approve(args.test_cmd, ctx)
    const body = { goal: args.goal, repo: ctx.worktree, test_cmd: args.test_cmd, plan }
    const r = await call("POST", "/v1/airlie/runs", body)
    if (ctx.abort.aborted) throw await cancelled("airlie_run", r.job_id)
    return `Started ${r.job_id}. Follow it with airlie_status.`
  },
})

export const status = tool({
  description:
    "Follow an airlie job (a studio run or a story): what happened since the last call, and the result once " +
    "it ends. Waits up to `wait` seconds (default 120) for news; call it again until the state is done, " +
    "stopped, failed or cancelled. A story that stops ends as a done job whose result has `blocked` set; the " +
    "state then reads \"stopped: <reason>\": it did not finish.",
  args: {
    job_id: tool.schema.string().describe("the id airlie_run or airlie_story returned"),
    wait: tool.schema.number().optional().describe("seconds to wait for news, at most 240"),
  },
  async execute(args, ctx) {
    const wait = Math.min(Math.max(args.wait ?? 120, 0), 240)
    const r = await follow(args.job_id, wait, ctx.abort)
    const stopped = r.state === "done" && r.result?.blocked // a stopped story is a done job that says why
    const state = stopped ? `stopped: ${String(r.result.blocked).replace(/\s+/g, " ")}` : r.state
    const out = [`${args.job_id}: ${state}`, ...r.events.map(line)]
    if (r.result) out.push("", "Result:", JSON.stringify(r.result, null, 1))
    if (r.error) out.push("", `Error: ${r.error}`)
    return out.join("\n")
  },
})

export const cancel = tool({
  description: "Stop a running airlie job (a studio run or a story). Work already committed stays on its branch.",
  args: { job_id: tool.schema.string().describe("the job to stop") },
  async execute(args, ctx) {
    const r = await call("DELETE", `/v1/airlie/jobs/${encodeURIComponent(args.job_id)}`, undefined, ctx.abort)
    return r.cancelled ? `Cancelled ${args.job_id}.` : `${args.job_id} was not running.`
  },
})

export const story = tool({
  description:
    "Run the story pipeline in this repository: a Jira issue (key like AIR-7, or its link) or a written request " +
    "is refined into a story, split into layers, built test-first one branch per layer, and opened as stacked " +
    "draft PRs (the issue moves to In Review). Only a human merges. Returns a job id for airlie_status.",
  args: {
    card: tool.schema.string().optional().describe("Jira issue key (AIR-7) or its link"),
    text: tool.schema.string().optional().describe("the request, when there is no card"),
    test_cmd: tool.schema.string().optional().describe("command every layer must pass (default: story.test_cmd)"),
  },
  async execute(args, ctx) {
    if (!args.card === !args.text) throw new Error("pass exactly one of card or text")
    await approve(args.test_cmd, ctx)
    const body = { card: args.card, text: args.text, repo: ctx.worktree, test_cmd: args.test_cmd }
    const r = await call("POST", "/v1/airlie/stories", body)
    if (ctx.abort.aborted) throw await cancelled("airlie_story", r.job_id)
    return `Started ${r.job_id}. Follow it with airlie_status.`
  },
})
