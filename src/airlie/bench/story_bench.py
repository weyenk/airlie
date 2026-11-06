"""The story bench (task bench:story): a fixed request built end to end by the story pipeline, measured.

A case (evals/stories/<id>.yaml) names a written request, the repository it is built in, the commit its first
runs built on, an oracle for the request's spec and the file the mutation score plants its bugs in. A run
builds it on a state directory of its own (no lessons or board from earlier runs, so runs compare), then reads
what it cost from the story's events and the studio runs' steps (phases, tokens per second by role, retries),
grades the top layer's branch (its gate, the oracle, the mutation score of its own tests, its tests and
assertions), closes the PRs it published and deletes their branches, and writes one JSON record. Two records
compare (`compare`): the deltas, and the case's budgets as regression lines. Born of the planning-line
benchmark of 2026-10-25/26 (docs/bench/2026-10-26-planning-line.md).

For the reliability loop (2026-11-06: real stories rerun from scratch until each goes through first try) a
case can publish nothing (`publish: false`: story.publish none, no GitHub), build on its own pinned base
(`pin_base`), in a clone of its own (`clone`), with no oracle; each record says whether the story went
through first try and what kept it from that (`tries`), and a sweep runs cases one after another
(`run_case`, `sweep_lines`)."""

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
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from airlie.story.cleanup import drop_every_draft_ref
from airlie.story.cleanup import local_branches as local_branches
from airlie.story.cleanup import prune_stale_stacks as prune_stale_stacks

if TYPE_CHECKING:
    from airlie.config import AirlieConfig
    from airlie.story.state import StoryState

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
    oracle: Path | None
    """The script that holds the top branch to the written spec; None for a story with no exact answer (the
    reliability cases of 2026-11-06): its quality is then the gate and the layers' own tests."""
    mutate: str | None
    """The Python file the mutation score plants its bugs in; None for a case built in another language (the
    score's mutants are Python's)."""
    budgets: dict[str, Any] = field(default_factory=dict)
    gate_timeout_s: float = 300
    """How long grading waits for the gate: airlie's own (pytest and the web build) takes minutes."""
    publish: bool = True
    """False: the run ends once every layer is built (or satisfied) and gated; nothing is pushed, no PR is
    opened and GitHub is never asked (story.publish: none), so a case can be rerun as often as it takes."""
    pin_base: bool = False
    """True: the story builds on `base` itself, not on the remote's trunk as it is when the run starts, so
    every run of the case builds on the same code however far the trunk has moved."""
    clone: Path | None = None
    """The checkout the story is built in; None: story.repos_root / name. A case in airlie itself names one of
    its own: repos_root / airlie is the home checkout serve runs from."""

    @property
    def name(self) -> str:
        """The repository's name: its clone is story.repos_root / name unless the case names one."""
        return self.repo.split("/")[-1]

    def checkout(self, repos_root: Path) -> Path:
        """Where the case is built: its own clone, else story.repos_root / name; ~ expanded."""
        return (self.clone or repos_root / self.name).expanduser()


def load_case(path: Path) -> StoryCase:
    """The case in `path`. Its request is kept as written (a folded `>-` request is one line; a literal `|`
    one keeps its lines, as a card's description does), stripped at both ends."""
    data = yaml.safe_load(path.read_text())
    return StoryCase(
        id=str(data["id"]),
        request=str(data["request"]).strip(),
        repo=str(data["repo"]),
        base=str(data["base"]),
        oracle=path.parent / str(data["oracle"]) if data.get("oracle") else None,
        mutate=str(data["mutate"]) if data.get("mutate") else None,
        budgets=dict(data.get("budgets") or {}),
        gate_timeout_s=float(data.get("gate_timeout_s") or 300),
        publish=bool(data.get("publish", True)),
        pin_base=bool(data.get("pin_base", False)),
        clone=Path(str(data["clone"])) if data.get("clone") else None,
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


TRY_COUNTS = ("package_tries", "packages_returned", "layer_builds", "replans", "gate_failures", "blocks",
              "answer_after_cuts", "last_turn_tool_calls")  # fmt: skip
"""The story-wide counts of `tries`, in the order a comparison shows them."""


def tries(story_dir: Path, runs_dir: Path) -> dict[str, Any]:
    """How far the story at `story_dir` was from going through first try (the reliability loop of
    2026-11-06), from its events and its studio runs': per layer, the package tries (a draft's try counts,
    `package_returned` or not), the kinds of each package returned, the builds, why each re-plan was asked
    and the gate failures inside its builds; story-wide, those summed, the blocks and the steering events
    (`answer_after_cuts`, `last_turn_tool_call`); and `first_try`: every layer packaged on try 1 (none
    returned), built on build 1 (no re-plan, no gate failure) or satisfied, no block, and the story reached
    review. `why_not` says, one line each, what kept it from that."""
    ev = _events(story_dir / "events.jsonl")
    layers: dict[str, dict[str, Any]] = {}
    open_try: dict[str, bool] = {}
    builds: dict[str, str] = {}  # a layer build's studio run -> its layer

    def layer(e: dict[str, Any]) -> dict[str, Any]:
        return layers.setdefault(
            str(e.get("layer") or "?"),
            {"package_tries": 0, "returned": [], "builds": 0, "replanned": [], "gate_failures": 0,
             "built": False, "satisfied": False},
        )  # fmt: skip

    for e in ev:
        kind, nn = e["kind"], str(e.get("layer") or "?")
        if kind == "package_start":
            layer(e)["package_tries"] += 1
            open_try[nn] = True
        elif kind == "package_returned":
            if not open_try.get(nn):  # the drafting conversation's try: no package_start of its own
                layer(e)["package_tries"] += 1
            layer(e)["returned"].append([str(k) for k in e.get("kinds") or []])
            open_try[nn] = False
        elif kind in ("package_ready", "layer_satisfied"):
            open_try[nn] = False
            layer(e)["satisfied"] |= kind == "layer_satisfied"
        elif kind == "layer_start":
            layer(e)["builds"] += 1
            if e.get("studio_run"):
                builds[str(e["studio_run"])] = nn
        elif kind == "layer_replanned":
            layer(e)["replanned"].append(str(e.get("why")))
        elif kind == "layer_built":
            layer(e)["built"] = True
    steering = Counter[str]()
    runs = dict.fromkeys(
        str(e["studio_run"]) for e in ev if e["kind"] in ("run", "layer_start") and e.get("studio_run")
    )
    for run in runs:
        for row in _events(runs_dir / run / "events.jsonl"):
            steering[row["kind"]] += 1
            if row["kind"] == "gate_failed" and run in builds:
                layers[builds[run]]["gate_failures"] += 1
    done = [e for e in ev if e["kind"] == "story_done"]
    reached_end = bool(done) and done[-1].get("blocked") is None and done[-1].get("stage") == "review"
    blocks = sum(e["kind"] == "blocked" for e in ev)
    why_not: list[str] = []
    for nn, x in sorted(layers.items()):
        for kinds in x["returned"]:
            why_not.append(f"layer {nn}: package returned ({', '.join(kinds) or 'no kind'})")
        if x["builds"] > 1 or x["replanned"]:
            why_not.append(
                f"layer {nn}: {x['builds']} builds (re-planned: {', '.join(x['replanned']) or '-'})"
            )
        if x["gate_failures"]:
            why_not.append(f"layer {nn}: {x['gate_failures']} gate failure(s) in its builds")
    if blocks:
        why_not.append(f"blocked {blocks} time(s)")
    if not reached_end:
        why_not.append("the story did not reach review")
    return {
        "first_try": not why_not,
        "why_not": why_not,
        "reached_end": reached_end,
        "package_tries": sum(x["package_tries"] for x in layers.values()),
        "packages_returned": sum(len(x["returned"]) for x in layers.values()),
        "returned_kinds": dict(Counter(k for x in layers.values() for kinds in x["returned"] for k in kinds)),
        "layer_builds": sum(x["builds"] for x in layers.values()),
        "replans": sum(len(x["replanned"]) for x in layers.values()),
        "gate_failures": sum(x["gate_failures"] for x in layers.values()),
        "blocks": blocks,
        "answer_after_cuts": steering["answer_after_cuts"],
        "last_turn_tool_calls": steering["last_turn_tool_call"],
        "layers": dict(sorted(layers.items())),
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


def grade(
    case: StoryCase,
    clone: Path,
    sha: str,
    gate: Sequence[str],
    layer_tests: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """The quality of commit `sha` of `clone`, in a throwaway checkout of it. `layer_tests` are the tests each
    built layer's package wrote (by layer). A case with no oracle is graded by its gate and those tests
    only."""
    tests = {nn: list(ids) for nn, ids in sorted((layer_tests or {}).items())}
    with tempfile.TemporaryDirectory(prefix=f"bench-{case.id}-") as tmp:
        checkout = Path(tmp) / case.name
        subprocess.run(["git", "clone", "-q", str(clone), str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "checkout", "-q", sha], check=True)
        gate_passes = _passes(checkout, gate, timeout_s=case.gate_timeout_s)
        if case.oracle is None:
            return {"gate_passes": gate_passes, "layer_tests": tests}
        target = checkout / case.mutate if case.mutate else None
        mutable = target is not None and target.is_file()
        return {
            "gate_passes": gate_passes,
            "layer_tests": tests,
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
    if case.budgets.get("oracle") == "all" and quality is not None and quality.get("oracle") is not None:
        passed, total = quality["oracle"]["spec"]
        if passed < total or total == 0:
            lines.append(f"the oracle passes {passed}/{total} of the spec: {quality['oracle']['failed']}")
    if quality is not None and quality.get("gate_passes") is False:
        lines.append("the top branch fails its gate")
    if case.budgets.get("first_try") and record.get("first_try") is False:
        why = (record.get("tries") or {}).get("why_not") or []
        lines.append(f"not first try: {'; '.join(why)}")
    return lines


def compare(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """What changed from record `a` to record `b`: wall time, the phases both reached, tokens, retries and
    quality, one line each."""

    def delta(x: float, y: float) -> str:
        return f"{x:g} -> {y:g} ({(y - x) / x * 100:+.0f}%)" if x else f"{x:g} -> {y:g}"

    lines = [f"wall min: {delta(a['cost']['wall_min'], b['cost']['wall_min'])}"]
    if "first_try" in a and "first_try" in b:  # side by side whether or not they changed: the loop's measure
        lines.append(f"first_try: {_yes(a['first_try'])} -> {_yes(b['first_try'])}")
        ta, tb = a.get("tries") or {}, b.get("tries") or {}
        lines += [f"{key}: {ta.get(key, 0)} -> {tb.get(key, 0)}" for key in TRY_COUNTS]
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
    if "gate_passes" in qa and "gate_passes" in qb and qa["gate_passes"] != qb["gate_passes"]:
        lines.append(f"gate passes: {_yes(qa['gate_passes'])} -> {_yes(qb['gate_passes'])}")
    if qa.get("oracle") and qb.get("oracle"):
        lines.append(f"oracle spec: {qa['oracle']['spec']} -> {qb['oracle']['spec']}")
    if qa.get("mutation") and qb.get("mutation"):
        ma, mb = qa["mutation"], qb["mutation"]
        lines.append(f"mutation: {ma['killed']}/{ma['total']} -> {mb['killed']}/{mb['total']}")
    return lines


def _yes(flag: object) -> str:
    return "yes" if flag else "no"


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


def remove_run_worktrees(clone: Path, under: Path) -> list[str]:
    """Remove the clone's worktrees under `under` (the run's state directory): a layer that stopped keeps its
    worktree for inspection, and a branch checked out there cannot be deleted. The paths removed."""
    out = subprocess.run(
        ["git", "-C", str(clone), "worktree", "list", "--porcelain"], capture_output=True, text=True
    )
    root = under.resolve()
    gone: list[str] = []
    for line in out.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line.removeprefix("worktree "))
        if path.resolve().is_relative_to(root):
            subprocess.run(
                ["git", "-C", str(clone), "worktree", "remove", "--force", str(path)], capture_output=True
            )
            gone.append(str(path))
    subprocess.run(["git", "-C", str(clone), "worktree", "prune"], capture_output=True)
    return gone


def _common_dir(path: Path) -> Path | None:
    """The git directory every worktree of the repository at `path` shares; None outside one."""
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True,
    )  # fmt: skip
    found = out.stdout.strip()
    return Path(found).resolve() if out.returncode == 0 and found else None


def clone_problems(
    case: StoryCase, clone: Path, running: Sequence[Path], source: str | None = None
) -> list[str]:
    """What keeps `clone` from building `case`, after making it when it is missing (`git clone` of `source`,
    by default the case's repository on GitHub over HTTPS: a fetch, never a push). It must not be a checkout
    of the airlie that runs the bench (`running`: its working directory and its source), whose branches and
    worktrees a run would share with serve (2026-11-06: a case in weyenk/airlie resolves to repos_root /
    airlie, the home checkout), and its origin must name the case's repository."""
    from airlie.story.github_app import GitHubAppError, repo_slug
    from airlie.teams.worktree import remote_url

    if not clone.exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        url = source or f"https://github.com/{case.repo}.git"
        made = subprocess.run(["git", "clone", "-q", url, str(clone)], capture_output=True, text=True)
        if made.returncode != 0:
            return [f"could not clone {url} into {clone}: {made.stderr.strip()[-300:]}"]
    mine = _common_dir(clone)
    if mine is None:
        return [f"{clone} is not a git repository: the case {case.id} is built in a clone of {case.repo}"]
    for path in running:
        if _common_dir(path) == mine:
            return [
                f"{clone} is the airlie checkout this bench runs from ({path}): a run would share its "
                "branches and worktrees with serve; give the case a clone of its own (clone: in "
                f"evals/stories/{case.id}.yaml)"
            ]
    origin = remote_url(clone, "origin")
    try:
        named = repo_slug(origin) if origin is not None else None
    except GitHubAppError:
        named = None
    if named is None or named.lower() != case.repo.lower():
        return [f"{clone}'s origin names {named or 'no github.com repository'}, not {case.repo}"]
    return []


def previous_record(out_dir: Path, case_id: str) -> Path | None:
    """The latest record of `case_id` in `out_dir` (story-<case>-<YYYYmmdd-HHMMSS>.json; a hand-named
    baseline is not one); None when there is none."""
    pattern = re.compile(rf"story-{re.escape(case_id)}-\d{{8}}-\d{{6}}\.json")
    found = sorted(p for p in out_dir.glob(f"story-{case_id}-*.json") if pattern.fullmatch(p.name))
    return found[-1] if found else None


class BenchSetupError(Exception):
    """A case that cannot run: what is wrong, one line each."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def run_case(
    cfg: AirlieConfig,
    case: StoryCase,
    *,
    state_root: Path,
    out_dir: Path,
    run: Callable[[Coroutine[Any, Any, StoryState]], StoryState],
    keep_prs: bool = False,
    publish: bool | None = None,
    say: Callable[[str], None] = print,
) -> tuple[dict[str, Any], Path]:
    """One run of `case`, written as a record in `out_dir`: built end to end on a fresh state directory under
    `state_root` (`cfg` is changed to it), measured, graded, its PRs closed and its branches and worktrees
    deleted unless `keep_prs`. `publish` (None: the case's) False runs with story.publish: none, so nothing
    reaches GitHub. `run` drives the story's coroutine (the CLI's stoppable asyncio.run). A case that cannot
    start is a BenchSetupError; a run that is stopped is cleared up and raised again. Moved here from the CLI
    for the sweep (2026-11-06)."""
    from airlie.story import factory
    from airlie.story.model import StorySetupError

    publish = case.publish if publish is None else publish
    state = fresh_state_dir(state_root.expanduser(), case.id)
    cfg.observability.state_dir = state  # no lessons, board or runs of an earlier run
    if not publish:
        cfg.story.publish = "none"
    clone = case.checkout(cfg.story.repos_root)
    problems = factory.story_problems(cfg)
    problems += clone_problems(case, clone, [Path.cwd(), Path(__file__).parent])
    if not problems:
        try:
            clone = factory.written_repo(cfg, clone)
        except StorySetupError as e:
            problems.append(str(e))
    if problems:
        raise BenchSetupError(problems)
    pin = case.base if case.pin_base else None
    say(f"{case.id}: building in {clone}, state in {state}" + ("" if publish else ", publishing nothing"))

    async def go() -> StoryState:
        board, card = await factory.card_for(cfg, None, case.request)
        return await factory.run_one(cfg, board=board, card=card, repo=clone, test_cmd=None, pinned_base=pin)

    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    # the build that runs, read before it does: a commit made during an hour-long run is not this run's code
    # (2026-10-31: two runs were stamped with commits made while they ran)
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    build = head.stdout.strip()
    if publish:
        prune_remote_branches(clone)  # no stale record of a branch an earlier run deleted on GitHub
    prune_stale_stacks(clone)  # nor of a stack whose branches an earlier run deleted
    before = local_branches(clone)
    cleared: dict[str, list[str]] = {}
    done = False
    try:
        st = run(go())
        record = _record(case, st, clone, state, started, build, publish, pin, keep_prs)
        done = True
    finally:
        # however the run or its grading ended (a stop, a grading clone that fails, the oracle's timeout, gh):
        # its branches and worktrees go, or the next run of the case, with the same branch names, stops at its
        # first layer (PR #247's review, 2026-11-06); an error is raised again after
        if not keep_prs or not done:
            cleared["worktrees_removed"] = remove_run_worktrees(clone, state)
            cleared["branches_deleted"] = delete_new_branches(clone, before)
            cleared["draft_refs_dropped"] = drop_every_draft_ref(
                clone
            )  # the drafts' snapshots, as at publish
            prune_stale_stacks(clone)
    record.update(cleared)
    return record, save(record, out_dir)


def _record(
    case: StoryCase,
    st: StoryState,
    clone: Path,
    state: Path,
    started: str,
    build: str,
    publish: bool,
    pin: str | None,
    keep_prs: bool,
) -> dict[str, Any]:
    """`run_case`'s record of the story `st` it ran: measured, graded and, unless kept, its PRs closed."""
    gate = " && ".join((st.gate or {}).get("commands") or [])
    top = st.built[max(st.built)]["sha"] if st.built else None
    built_on = (st.base or {}).get("sha")
    layer_tests = {nn: [str(t) for t in b.get("tests") or []] for nn, b in st.built.items()}
    measured = tries(st.path, state / "runs")
    record: dict[str, Any] = {
        "case": case.id,
        "started": started,
        "airlie_build": build,
        "publish": publish,
        "base": {
            "expected": case.base,
            "built_on": built_on,
            "same": built_on == case.base,
            "pinned": pin is not None,
        },
        "stage": st.stage,
        "blocked": st.blocked,
        "first_try": measured["first_try"],
        "tries": measured,
        "layers_built": len(st.built),
        "layers_satisfied": len(st.satisfied),
        "prs": [p.get("url") for p in st.prs],
        "cost": measure(st.path, state / "runs"),
        "quality": grade(case, clone, top, ["sh", "-c", gate], layer_tests) if top and gate else None,
    }
    record["closed"] = close_prs(case.repo, st.prs, clone) if publish and not keep_prs else []
    return record


@dataclass
class SweepRow:
    """One case of a sweep: its record (None when it could not run, `error` says why) and the record before
    it, if any."""

    case: str
    record: dict[str, Any] | None
    error: str | None = None
    previous: dict[str, Any] | None = None
    previous_name: str | None = None


def sweep_lines(rows: Sequence[SweepRow]) -> list[str]:
    """A sweep's summary: one row per case (first try, wall minutes, package tries, builds, blocks, gate
    failures and the first try of its previous record), then each case's comparison with that record."""
    head = ("case", "first_try", "wall_min", "tries", "builds", "blocks", "gate_fails", "before")
    table: list[tuple[str, ...]] = [head]
    for row in rows:
        before = _yes(row.previous["first_try"]) if row.previous and "first_try" in row.previous else "-"
        if row.record is None:
            table.append((row.case, "error", "-", "-", "-", "-", "-", before))
            continue
        t = row.record.get("tries") or {}
        table.append((
            row.case, _yes(row.record.get("first_try")), f"{row.record['cost']['wall_min']:g}",
            str(t.get("package_tries", "-")), str(t.get("layer_builds", "-")), str(t.get("blocks", "-")),
            str(t.get("gate_failures", "-")), before,
        ))  # fmt: skip
    widths = [max(len(r[i]) for r in table) for i in range(len(head))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(r, widths, strict=True)).rstrip() for r in table]
    for row in rows:
        if row.error is not None:
            lines.append(f"{row.case}: {row.error}")
        elif row.record is not None and row.previous is not None:
            lines.append(f"{row.case} vs {row.previous_name}:")
            lines += [f"  {line}" for line in compare(row.previous, row.record)]
    return lines
