// Cards shared by the chat and the start form: a plan to approve, a job that was started.
import { request } from "./api.js";
import { chip, h } from "./dom.js";

/** The chief's plan ({assumptions, subtasks[{id, role, brief, depends_on}]}) as a card. `onApprove(plan)` is
 * called when the person approves; `onChange()` when they want to change it. Either may be omitted. */
export function planCard(plan, { onApprove, onChange, approved = false } = {}) {
  const subtasks = Array.isArray(plan?.subtasks) ? plan.subtasks : [];
  const assumptions = Array.isArray(plan?.assumptions) ? plan.assumptions : [];
  const error = h("span", { class: "error" });
  const approve = h("button", { type: "button", class: "btn primary" }, "Approve");
  const change = h("button", { type: "button", class: "btn" }, "Change");
  const buttons = h("div", { class: "row plan-actions" }, approve, change);
  approve.addEventListener("click", async () => {
    approve.disabled = change.disabled = true;
    error.textContent = "";
    try {
      await onApprove?.(plan);
      buttons.replaceWith(chip("ok", "Approved"));
    } catch (e) {
      error.textContent = e?.name === "AuthError" ? "Sign in again, then approve." : e.message;
      approve.disabled = change.disabled = false;
    }
  });
  change.addEventListener("click", () => onChange?.());
  return h(
    "div",
    { class: "plan-card stack" },
    h("span", null, subtasks.length ? `Here's the plan, ${subtasks.length} step${subtasks.length === 1 ? "" : "s"}:` : "A plan with no steps."),
    h(
      "ol",
      { class: "plan-steps" },
      subtasks.map((s) =>
        h(
          "li",
          null,
          h("span", { class: "mono small muted" }, `${s.id ?? "?"} · ${s.role ?? "any"}`),
          h("div", null, s.brief ?? ""),
          s.depends_on?.length ? h("span", { class: "small muted" }, `after ${s.depends_on.join(", ")}`) : null,
        ),
      ),
    ),
    assumptions.length
      ? h("details", { class: "small muted" }, h("summary", null, `${assumptions.length} assumption${assumptions.length === 1 ? "" : "s"}`), h("ul", null, assumptions.map((a) => h("li", null, a))))
      : null,
    approved ? chip("ok", "Approved") : onApprove ? buttons : null,
    error,
  );
}

/** A started job: links to its screen. `job` = {job_id, kind, label, repo}. */
export function jobCard(job) {
  const kind = job.kind === "plan" ? "Planning" : job.kind === "story" ? "Story started" : "Job started";
  return h(
    "a",
    { href: `#/jobs/${encodeURIComponent(job.job_id)}`, class: "job-card" },
    h("span", { class: "stack small", style: { gap: "2px" } }, h("strong", null, kind), h("span", { class: "mono muted" }, job.label || job.job_id)),
    chip("run", "Watch"),
  );
}

/** Start a studio run from an approved plan. */
export function startRun({ goal, repo, plan, test_cmd }) {
  const body = { goal, repo, plan };
  if (test_cmd) body.test_cmd = test_cmd;
  return request("/v1/airlie/runs", { method: "POST", body });
}
