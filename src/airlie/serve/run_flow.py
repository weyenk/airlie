"""A run of any topology as the workflow view draws it, in the shape of a story's flow (airlie.serve.flow) so
one view draws both: the stages its topology runs, left to right, each with a lane of jobs, one per agent in
the run's tree (airlie.serve.tree). studio: the chief's plan, the specialists, the reviewer, the synthesis;
teams: the teams side by side, then the judge; single: the one agent; fork: the agent until it got stuck,
then its forks. A story is not a topology: it runs on the studio, one studio run per layer, and has its own
flow. Derived from the run's events.jsonl and trace alone, like the tree."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from airlie.serve.flow import _job, final_text, trace_of
from airlie.serve.running import run_progress
from airlie.serve.tree import RunNotFound, run_tree

JOB_STATUS = {
    "waiting": "pending",
    "active": "running",
    "idle": "running",
    "stuck": "running",
    "done": "passed",
    "failed": "failed",
}
"""A job's status by the state of its node in the tree (every state the tree gives a node)."""


def _stages(tree: dict[str, Any]) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """(id, label, tree nodes) of each stage the run's topology runs, in order; none before its run_start."""

    def of(kind: str) -> list[dict[str, Any]]:
        return [n for n in tree["nodes"] if n["kind"] == kind]

    stages: list[tuple[str, str, list[dict[str, Any]]]] = []
    if tree["topology"] == "studio":
        stages.append(("plan", "Plan", of("chief")))
        if tree["mode"] == "plan":  # a plan job stops at the plan, for a person to approve
            return stages
        stages.append(("specialists", "Specialists", of("role")))
        if review := of("reviewer"):
            stages.append(("review", "Review", review))
        if synthesis := of("synthesis"):
            stages.append(("synthesis", "Synthesis", synthesis))
    elif tree["topology"] == "teams":
        stages.append(("teams", "Teams", of("team")))
        if judge := of("aggregate"):
            stages.append(("judge", "Judge", judge))
    elif tree["topology"] == "single":
        stages.append(("agent", "Agent", of("team")))
    elif tree["topology"] == "fork":
        branches = of("branch")  # the agent first, then its forks in the order the fork points named them
        stages.append(("agent", "Agent", branches[:1]))
        if branches[1:]:
            stages.append(("forks", "Forks", branches[1:]))
    return stages


def _stage_status(jobs: list[dict[str, Any]]) -> str:
    statuses = {j["status"] for j in jobs}
    if "failed" in statuses:
        return "failed"
    if "running" in statuses:
        return "running"
    return "passed" if statuses == {"passed"} else "pending"


def run_flow(state_dir: Path, run_id: str, *, models: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The flow of run `run_id` under `state_dir/runs`: its `nodes` (stages) left to right, each with its
    `jobs`, made as a story's are; `stage` is "running" until the run's run_done, then "done", and `progress`
    the share of its agents finished (None for one agent). RunNotFound for an id that names no run directory.
    `models` is passed to the tree (airlie.serve.tree.build_tree)."""
    tree = run_tree(state_dir, run_id, models=models)
    nodes: list[dict[str, Any]] = []
    for stage_id, label, members in _stages(tree):
        jobs = [
            _job(
                n["id"],
                n["label"],
                n["role"],
                JOB_STATUS[n["state"]],
                studio_run=run_id,
                tokens=n["tokens"],
                fix_rounds=n["fix_rounds"],
                won=n["won"],
            )
            for n in members
        ]
        nodes.append(
            {"id": stage_id, "label": label, "kind": "stage", "status": _stage_status(jobs), "jobs": jobs}
        )
    return {
        "key": run_id,
        "kind": "run",
        "topology": tree["topology"],
        "mode": tree["mode"],
        "title": tree["goal"],
        "summary": None,
        "stage": tree["state"],
        "blocked": None,
        "acceptance": [],
        "ok": tree["ok"],
        "progress": run_progress(tree),
        "nodes": nodes,
    }


def run_job_detail(state_dir: Path, run_id: str, node_id: str, job_id: str) -> dict[str, Any]:
    """One job of a run's flow, with the keys of a story job's detail (airlie.serve.flow.job_detail): the job,
    what it was opened with, what it said last and the steps it took, from runs/<run_id>/trace/<job_id>/.
    RunNotFound for a run, stage or job that is not there."""
    flow = run_flow(state_dir, run_id)
    node = next((n for n in flow["nodes"] if n["id"] == node_id), None)
    job = next((j for j in (node or {}).get("jobs", []) if j["id"] == job_id), None)
    if node is None or job is None:
        raise RunNotFound(f"no job '{job_id}' in '{node_id}' of run '{run_id}'")
    prompt, steps = trace_of(state_dir, run_id, job_id)
    return {
        "node": node_id,
        "job": job,
        "input": prompt,
        "input_note": None if prompt is not None else "opening prompt not recorded",
        "output": {"final_text": final_text(steps)},
        "steps": steps,
    }
