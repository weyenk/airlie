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
READ_TOOLS = frozenset({"read_file", "list_dir", "grep"})
"""The tools that only look at the repository: the reads a dossier would save (the performance plan's P2)."""
CONTEXT_BUCKETS = ((16_000, "<16k"), (48_000, "16-48k"), (None, ">48k"))
"""Decode speed is read per prompt size: it falls as the context grows (the plan's P3)."""


@dataclass(frozen=True)
class StoryCase:
    """One case of the bench: evals/stories/<id>.yaml."""

    id: str
    request: str
    repo: str
    base: str
    oracle: Path
    mutate: str | None
    """The Python file the mutation score plants its bugs in; None for a case built in another language (the
    score's mutants are Python's)."""
    budgets: dict[str, Any] = field(default_factory=dict)
    gate_timeout_s: float = 300
    """How long grading waits for the gate: airlie's own (pytest and the web build) takes minutes."""

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
        mutate=str(data["mutate"]) if data.get("mutate") else None,
        budgets=dict(data.get("budgets") or {}),
        gate_timeout_s=float(data.get("gate_timeout_s") or 300),
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
    reads: int = 0
    """Calls of a READ_TOOLS tool."""
    thinking_tokens: int = 0
    """Of tokens_out, the share spent thinking: estimated from the step's reasoning and answer lengths (steps
    recorded before 2026-10-27 have none, and count as answer)."""

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
        return {
            "wall_min": 0.0,
            "idle_min": 0.0,
            "decode_tps_by_context": {},
            "phases": [],
            "roles": [],
            "retries": {},
        }
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

    calls: list[tuple[float, float]] = []  # each model call's (start, end), for the idle time
    buckets: dict[str, list[float]] = {name: [0.0, 0.0] for _, name in CONTEXT_BUCKETS}  # tokens, seconds
    for run, phase in stage_of.items():
        rows = _events(runs_dir / run / "events.jsonl")
        done = [r for r in rows if r["kind"] == "run_done"]
        # a `reply` is a model call that ran no tool (a package handed in, a verdict): a call all the same
        steps = [r for r in rows if r["kind"] in ("step", "reply") and r.get("gen_ms") is not None]
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
            c.reads += s.get("tool") in READ_TOOLS
            c.thinking_tokens += _thinking(s)
            end = float(s["ts"]) - float(s.get("tool_ms") or 0) / 1000
            calls.append((end - (float(s.get("prefill_ms") or 0) + float(s["gen_ms"])) / 1000, end))
            bucket = buckets[_bucket(int(s.get("prompt_tokens") or 0))]
            bucket[0] += int(s.get("completion_tokens") or 0)
            bucket[1] += float(s["gen_ms"]) / 1000
    for t in ev:
        if t["kind"] == "amigos_turn" and t["role"] != "airlie":  # the meeting's turns are not studio steps
            c = cost("meeting", str(t["role"]))
            c.calls += 1
            c.tokens_in += int(t["prompt_tokens"])
            c.tokens_out += int(t["completion_tokens"])
            c.wall_s += float(t["wall_ms"]) / 1000
            calls.append((float(t["ts"]) - float(t["wall_ms"]) / 1000, float(t["ts"])))
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
    wall_s = ev[-1]["ts"] - t0
    return {
        "wall_min": round(wall_s / 60, 1),
        "idle_min": round(max(wall_s - _covered(calls), 0.0) / 60, 1),
        "decode_tps_by_context": {
            name: round(tokens / secs, 1) if secs > 1 else None for name, (tokens, secs) in buckets.items()
        },
        "phases": phases,
        "roles": roles,
        "retries": retries,
    }


def _thinking(step: dict[str, Any]) -> int:
    """A step's thinking tokens: its completion tokens in the share its reasoning has of what it wrote."""
    thought, said = int(step.get("reasoning_chars") or 0), int(step.get("answer_chars") or 0)
    return round(int(step.get("completion_tokens") or 0) * thought / (thought + said)) if thought else 0


def _bucket(prompt_tokens: int) -> str:
    return next(name for top, name in CONTEXT_BUCKETS if top is None or prompt_tokens < top)


def _covered(spans: Sequence[tuple[float, float]]) -> float:
    """The seconds at least one of `spans` covers: model calls made at once count once."""
    total, reach = 0.0, float("-inf")
    for start, end in sorted(spans):
        if end > reach:
            total += end - max(start, reach)
            reach = end
    return total


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
    """The test functions and the assertions of the checkout's tests/ (test_*.py), and its Vitest tests
    (`it(` or `test(` calls, and `expect(`s) in *.test.ts files outside node_modules."""
    tests = asserts = 0
    for path in (checkout / "tests").rglob("test_*.py"):
        text = path.read_text()
        tests += len(re.findall(r"^\s*(?:async\s+)?def test_", text, re.MULTILINE))
        asserts += len(re.findall(r"\bassert\w*\b", text))
    for path in checkout.rglob("*.test.ts"):
        if "node_modules" not in path.parts:
            text = path.read_text()
            tests += len(re.findall(r"(?<![\w.])(?:it|test)(?:\.\w+)*\s*\(", text))
            asserts += len(re.findall(r"(?<![\w.])expect\s*\(", text))
    return {"tests": tests, "asserts": asserts}


def grade(case: StoryCase, clone: Path, sha: str, gate: Sequence[str]) -> dict[str, Any]:
    """The quality of commit `sha` of `clone`, in a throwaway checkout of it."""
    with tempfile.TemporaryDirectory(prefix=f"bench-{case.id}-") as tmp:
        checkout = Path(tmp) / case.name
        subprocess.run(["git", "clone", "-q", str(clone), str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "checkout", "-q", sha], check=True)
        target = checkout / case.mutate if case.mutate else None
        mutable = target is not None and target.is_file()
        return {
            "gate_passes": _passes(checkout, gate, timeout_s=case.gate_timeout_s),
            "oracle": oracle(case.oracle, checkout),
            "mutation": mutation_score(checkout, case.mutate, gate) if case.mutate and mutable else None,
            **count_tests(checkout),
            "source_lines": len(target.read_text().splitlines()) if target is not None and mutable else 0,
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
    if "idle_min" in a["cost"] and "idle_min" in b["cost"]:
        lines.append(f"idle min: {delta(a['cost']['idle_min'], b['cost']['idle_min'])}")
    for key in ("tokens_in", "tokens_out", "thinking_tokens", "calls", "reads"):
        if all(key in r for x in (a, b) for r in x["cost"]["roles"]):
            before, after = (sum(r[key] for r in x["cost"]["roles"]) for x in (a, b))
            lines.append(f"{key}: {delta(before, after)}")
    ca, cb = (x["cost"].get("decode_tps_by_context") or {} for x in (a, b))
    lines += [
        f"decode tok/s {k}: {ca[k]} -> {cb[k]}" for k in cb if ca.get(k) is not None and cb[k] is not None
    ]
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


def local_branches(clone: Path) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(clone), "branch", "--format=%(refname:short)"], capture_output=True, text=True
    )
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def prune_remote_branches(clone: Path) -> None:
    """Drop the clone's records of remote branches that are gone: a branch a closed PR's run deleted on
    GitHub stays as a stale remote-tracking ref, and gh stack's push, leased on it, is refused as "stale info"
    when a later run publishes a branch of the same name (2026-10-26)."""
    subprocess.run(["git", "-C", str(clone), "fetch", "-q", "--prune", "origin"], capture_output=True)


def delete_new_branches(clone: Path, before: set[str]) -> list[str]:
    """Delete the clone's branches a run made (those not in `before`), built, blocked or published, so the
    next run can make them again: a written request's branch names are the same every run, and a branch a
    blocked run left stopped the next one at its first layer (2026-10-26). The names deleted."""
    made = sorted(local_branches(clone) - before)
    for branch in made:
        subprocess.run(["git", "-C", str(clone), "branch", "-D", branch], capture_output=True)
    return made


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
