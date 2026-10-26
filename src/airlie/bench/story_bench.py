"""The story bench (task bench:story): a fixed request built end to end by the story pipeline, measured.

A case (evals/stories/<id>.yaml) names a written request, the repository it is built in, the commit its first
runs built on, an oracle for the request's spec and the file the mutation score plants its bugs in. A run
builds it on a state directory of its own (no lessons or board from earlier runs, so runs compare), then reads
what it cost from the story's events and the studio runs' steps (phases, tokens per second by role, retries),
grades the top layer's branch (its gate, the oracle, the mutation score of its own tests, its tests and
assertions), closes the PRs it published and deletes their branches, and writes one JSON record. Two records
compare (`compare`): the deltas, and the case's budgets as regression lines. Born of the planning-line
benchmark of 2026-10-25/26 (docs/bench/2026-10-26-planning-line.md)."""

from __future__ import annotations

import ast
import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

PHASE_MARKS = ("survey_done", "plan_done", "amigos_agreed", "amigos_owner_decided", "packages_drafting",
               "layer_built", "published", "story_done")  # fmt: skip
"""The story events that end a phase, in the order a story reaches them."""


@dataclass(frozen=True)
class StoryCase:
    """One case of the bench: evals/stories/<id>.yaml."""

    id: str
    request: str
    repo: str
    base: str
    oracle: Path
    mutate: str
    budgets: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """The repository's name: its clone is story.repos_root / name."""
        return self.repo.split("/")[-1]


def load_case(path: Path) -> StoryCase:
    data = yaml.safe_load(path.read_text())
    return StoryCase(
        id=str(data["id"]),
        request=" ".join(str(data["request"]).split()),
        repo=str(data["repo"]),
        base=str(data["base"]),
        oracle=path.parent / str(data["oracle"]),
        mutate=str(data["mutate"]),
        budgets=dict(data.get("budgets") or {}),
    )


def find_case(cases_dir: Path, case_id: str) -> StoryCase:
    path = cases_dir / f"{case_id}.yaml"
    if not path.is_file():
        known = sorted(p.stem for p in cases_dir.glob("*.yaml"))
        raise FileNotFoundError(f"no story case {case_id!r} in {cases_dir} (known: {', '.join(known)})")
    return load_case(path)


# ---- what a run cost, from its events ----


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


@dataclass
class RoleCost:
    """What one role spent in one phase: calls, tokens in and out, prefill and decode seconds and wall
    seconds."""

    phase: str
    role: str
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    prefill_s: float = 0.0
    decode_s: float = 0.0
    wall_s: float = 0.0

    @property
    def decode_tps(self) -> float | None:
        """Tokens out per decode second; None where the steps record too little decode time to tell (the
        meeting's turns record wall time only; some coder steps record ~0.1 s of decode)."""
        return round(self.tokens_out / self.decode_s, 1) if self.decode_s > 1 else None

    @property
    def prefill_tps(self) -> float | None:
        return round(self.tokens_in / self.prefill_s) if self.prefill_s > 0 else None


def measure(story_dir: Path, runs_dir: Path) -> dict[str, Any]:
    """The cost of the story at `story_dir`: its wall minutes, where its phases ended (minutes from its
    start), each phase's cost by role and its retries."""
    ev = _events(story_dir / "events.jsonl")
    if not ev:
        return {"wall_min": 0.0, "phases": [], "roles": [], "retries": {}}
    t0 = ev[0]["ts"]
    stage_of: dict[str, str] = {}
    for e in ev:
        if e["kind"] == "run":
            stage_of[e["studio_run"]] = str(e["stage"])
        elif e["kind"] == "layer_start":
            stage_of[e["studio_run"]] = "layer build"
    costs: dict[tuple[str, str], RoleCost] = {}

    def cost(phase: str, role: str) -> RoleCost:
        return costs.setdefault((phase, role), RoleCost(phase, role))

    for run, phase in stage_of.items():
        rows = _events(runs_dir / run / "events.jsonl")
        done = [r for r in rows if r["kind"] == "run_done"]
        steps = [r for r in rows if r["kind"] == "step" and r.get("gen_ms") is not None]
        per_role = Counter(str(s.get("role")) for s in steps)
        wall = float(done[0]["wall_ms"]) / 1000 if done else 0.0
        for role, n in per_role.items():  # the run's wall time, shared by its roles by their steps
            cost(phase, role).wall_s += wall * n / max(sum(per_role.values()), 1)
        for s in steps:
            c = cost(phase, str(s.get("role")))
            c.calls += 1
            c.tokens_in += int(s.get("prompt_tokens") or 0)
            c.tokens_out += int(s.get("completion_tokens") or 0)
            c.prefill_s += float(s.get("prefill_ms") or 0) / 1000
            c.decode_s += float(s["gen_ms"]) / 1000
    for t in ev:
        if t["kind"] == "amigos_turn" and t["role"] != "airlie":  # the meeting's turns are not studio steps
            c = cost("meeting", str(t["role"]))
            c.calls += 1
            c.tokens_in += int(t["prompt_tokens"])
            c.tokens_out += int(t["completion_tokens"])
            c.wall_s += float(t["wall_ms"]) / 1000
    kinds = Counter(e["kind"] for e in ev)
    by_source = Counter(e.get("source") == "package" for e in ev if e["kind"] == "amigos_objection")
    retries = {
        "plan_rounds": kinds["plan_round"],
        "plan_fixes": kinds["plan_fix"],
        "meeting_turns": sum(1 for e in ev if e["kind"] == "amigos_turn" and e["role"] != "airlie"),
        "meeting_objections": by_source[False],
        "critic_objections": by_source[True],
        "package_attempts": kinds["package_start"],
        "packages_rejected": kinds["package_returned"],
        "unusable_replies": kinds["retry"],
        "layer_builds": kinds["layer_start"],
        "gate_replans": kinds["layer_replanned"],
        "supervisor_triages": kinds["supervisor_triage"],
    }
    phases = [
        {"at": e["kind"] + (f" {e['layer']}" if e.get("layer") else ""), "min": round((e["ts"] - t0) / 60, 1)}
        for e in ev
        if e["kind"] in PHASE_MARKS
    ]
    roles = [
        {**asdict(c), "decode_tps": c.decode_tps, "prefill_tps": c.prefill_tps}
        for c in sorted(costs.values(), key=lambda c: -c.wall_s)
        if c.calls or c.wall_s
    ]
    return {
        "wall_min": round((ev[-1]["ts"] - t0) / 60, 1),
        "phases": phases,
        "roles": roles,
        "retries": retries,
    }


# ---- how good it is, from its top branch ----

_SWAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}  # fmt: skip


def mutants(source: str) -> Iterator[tuple[str, str]]:
    """Small bugs in `source`, each as (where, mutated source): an integer one higher, a short upper-case
    string reversed or changed, a comparison flipped, and/or swapped, + and - swapped. Equivalent mutants are
    not removed: a score short of 100% is read with its survivors."""
    tree = ast.parse(source)
    plain = ast.unparse(tree)
    for n, node in enumerate(ast.walk(tree)):
        if not isinstance(node, ast.Constant | ast.Compare | ast.BoolOp | ast.BinOp):
            continue
        t = copy.deepcopy(tree)
        twin = next(x for m, x in enumerate(ast.walk(t)) if m == n)
        if isinstance(twin, ast.Constant) and type(twin.value) is int:
            twin.value += 1
        elif isinstance(twin, ast.Constant) and isinstance(twin.value, str) and 0 < len(twin.value) <= 3:
            if not twin.value.isupper():
                continue
            twin.value = twin.value[::-1] if len(twin.value) > 1 else ("V" if twin.value == "I" else "I")
        elif isinstance(twin, ast.Compare):
            twin.ops[0] = _SWAP.get(type(twin.ops[0]), ast.NotEq)()
        elif isinstance(twin, ast.BoolOp):
            twin.op = ast.Or() if isinstance(twin.op, ast.And) else ast.And()
        elif isinstance(twin, ast.BinOp) and isinstance(twin.op, ast.Add | ast.Sub):
            twin.op = ast.Sub() if isinstance(twin.op, ast.Add) else ast.Add()
        else:
            continue
        code = ast.unparse(t)
        if code != plain:
            yield f"{type(node).__name__} line {getattr(node, 'lineno', 0)}", code


def _passes(checkout: Path, gate: Sequence[str], timeout_s: float = 300) -> bool:
    try:
        return (
            subprocess.run(list(gate), cwd=checkout, capture_output=True, timeout=timeout_s).returncode == 0
        )
    except subprocess.TimeoutExpired:
        return False


def mutation_score(checkout: Path, target: str, gate: Sequence[str]) -> dict[str, Any]:
    """How many of `target`'s mutants the checkout's own tests (`gate`) catch; the file is put back after.
    A mutant that makes the tests run ten times as long as the clean run (at least 10 s) is caught:
    `n + value` for `n - value` in a `while n >= value` loop never ends."""
    path = checkout / target
    original = path.read_text()
    t0 = time.monotonic()
    _passes(checkout, gate)
    limit = max(10.0, 10 * (time.monotonic() - t0))
    total, killed, survived = 0, 0, []
    try:
        for where, code in mutants(original):
            path.write_text(code)
            total += 1
            if _passes(checkout, gate, timeout_s=limit):
                survived.append(where)
            else:
                killed += 1
    finally:
        path.write_text(original)
    return {"total": total, "killed": killed, "survived": survived}


def oracle(script: Path, checkout: Path) -> dict[str, Any]:
    """The case's oracle on the checkout, in its own process: passed and total, for the spec and beyond it,
    with the names of the spec's failures."""
    out = subprocess.run(
        [sys.executable, "-I", str(script), str(checkout)], capture_output=True, text=True, timeout=600
    )
    if out.returncode != 0:
        return {"spec": [0, 0], "extra": [0, 0], "failed": [f"the oracle did not run: {out.stderr[-300:]}"]}
    got = json.loads(out.stdout)
    return {
        "spec": [sum(ok for _, ok in got["spec"]), len(got["spec"])],
        "extra": [sum(ok for _, ok in got["extra"]), len(got["extra"])],
        "failed": [name for name, ok in got["spec"] if not ok],
    }


def count_tests(checkout: Path) -> dict[str, int]:
    """The test functions and the assertions of the checkout's tests/ (test_*.py)."""
    tests = asserts = 0
    for path in (checkout / "tests").rglob("test_*.py"):
        text = path.read_text()
        tests += len(re.findall(r"^\s*(?:async\s+)?def test_", text, re.MULTILINE))
        asserts += len(re.findall(r"\bassert\w*\b", text))
    return {"tests": tests, "asserts": asserts}


def grade(case: StoryCase, clone: Path, sha: str, gate: Sequence[str]) -> dict[str, Any]:
    """The quality of commit `sha` of `clone`, in a throwaway checkout of it."""
    with tempfile.TemporaryDirectory(prefix=f"bench-{case.id}-") as tmp:
        checkout = Path(tmp) / case.name
        subprocess.run(["git", "clone", "-q", str(clone), str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "checkout", "-q", sha], check=True)
        target = checkout / case.mutate
        return {
            "gate_passes": _passes(checkout, gate),
            "oracle": oracle(case.oracle, checkout),
            "mutation": mutation_score(checkout, case.mutate, gate) if target.is_file() else None,
            **count_tests(checkout),
            "source_lines": len(target.read_text().splitlines()) if target.is_file() else 0,
        }


# ---- a record, and two compared ----


def verdict(case: StoryCase, record: dict[str, Any]) -> list[str]:
    """The case's budgets against one record: a line per budget missed (none: the run is within them)."""
    lines: list[str] = []
    if record.get("blocked"):
        lines.append(f"blocked: {record['blocked']}")
    wall = case.budgets.get("wall_min")
    if wall is not None and record["cost"]["wall_min"] > float(wall):
        lines.append(f"wall time {record['cost']['wall_min']} min is over its budget of {wall}")
    quality = record.get("quality")
    if case.budgets.get("oracle") == "all" and quality is not None:
        passed, total = quality["oracle"]["spec"]
        if passed < total or total == 0:
            lines.append(f"the oracle passes {passed}/{total} of the spec: {quality['oracle']['failed']}")
    return lines


def compare(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """What changed from record `a` to record `b`: wall time, the phases both reached, tokens, retries and
    quality, one line each."""

    def delta(x: float, y: float) -> str:
        return f"{x:g} -> {y:g} ({(y - x) / x * 100:+.0f}%)" if x else f"{x:g} -> {y:g}"

    lines = [f"wall min: {delta(a['cost']['wall_min'], b['cost']['wall_min'])}"]
    pa = {p["at"]: p["min"] for p in a["cost"]["phases"]}
    lines += [
        f"{p['at']} at min: {delta(pa[p['at']], p['min'])}" for p in b["cost"]["phases"] if p["at"] in pa
    ]
    for key in ("tokens_in", "tokens_out"):
        before, after = (sum(r[key] for r in x["cost"]["roles"]) for x in (a, b))
        lines.append(f"{key}: {delta(before, after)}")
    for key, value in b["cost"]["retries"].items():
        before = a["cost"]["retries"].get(key, 0)
        if before != value:
            lines.append(f"{key}: {before} -> {value}")
    qa, qb = a.get("quality") or {}, b.get("quality") or {}
    for key in ("tests", "asserts", "source_lines"):
        if key in qa and key in qb and qa[key] != qb[key]:
            lines.append(f"{key}: {qa[key]} -> {qb[key]}")
    if qa.get("oracle") and qb.get("oracle"):
        lines.append(f"oracle spec: {qa['oracle']['spec']} -> {qb['oracle']['spec']}")
    if qa.get("mutation") and qb.get("mutation"):
        ma, mb = qa["mutation"], qb["mutation"]
        lines.append(f"mutation: {ma['killed']}/{ma['total']} -> {mb['killed']}/{mb['total']}")
    return lines


def close_prs(repo: str, prs: Sequence[dict[str, Any]], clone: Path) -> list[str]:
    """Close the run's PRs on GitHub, deleting their branches there, and delete them from the clone: the
    sandbox is left as the run found it. What could not be closed, one line each."""
    problems: list[str] = []
    for pr in prs:
        out = subprocess.run(
            ["gh", "pr", "close", str(pr["number"]), "-R", repo, "--delete-branch",
             "--comment", "airlie bench run: closed by the bench, not a change to review"],
            capture_output=True, text=True,
        )  # fmt: skip
        if out.returncode != 0:
            problems.append(f"#{pr['number']}: {out.stderr.strip()[-200:]}")
        subprocess.run(["git", "-C", str(clone), "branch", "-D", str(pr["branch"])], capture_output=True)
    return problems


def save(record: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"story-{record['case']}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(record, indent=1) + "\n")
    return path


def fresh_state_dir(root: Path, case_id: str) -> Path:
    """A state directory for one run: no lessons, board or runs of an earlier one."""
    path = root / case_id / time.strftime("%Y%m%d-%H%M%S")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path
